"""One scan cycle, and the loop that repeats it.

The order inside a cycle is deliberate. Open signals are graded against fresh
prices *before* any new signal is considered, so a coin that just hit its stop
is free to be re-scanned this cycle rather than next one, and so a data outage
can never mark a signal open for longer than it really was.

The kill-switch sits between analysis and firing: a degraded feed stops new
calls but never stops the tracker. Halting the tracker too would leave open
positions ungraded, which is the one thing worse than firing nothing.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime

from .alerts import NotifierGroup
from .config import Settings
from .exchange import Feed, FeedError, candles_are_stale
from .features import TechnicalFeatures, compute_features
from .fusion import fuse
from .legs import score_technical
from .levels import build_levels
from .models import (
    Candidate,
    LegScore,
    ScanReport,
    Signal,
    expiry_for,
    utcnow,
)
from .screen import setup_score, shortlist, stage1_universe
from .store import Store
from .tracker import update_open_signals

log = logging.getLogger(__name__)


class Scanner:
    def __init__(self, settings: Settings, feed: Feed, store: Store,
                 notifiers: NotifierGroup | None = None) -> None:
        self.settings = settings
        self.feed = feed
        self.store = store
        self.notifiers = notifiers or NotifierGroup([])
        # Kept from the last cycle so the coin detail view can show the chart
        # readings behind a score without refetching.
        self.last_features: dict[str, TechnicalFeatures] = {}

    # -- one cycle --------------------------------------------------------

    def run_cycle(self) -> ScanReport:
        started_at = utcnow()
        stats = getattr(self.feed, "stats", None)
        if stats is not None:
            stats.reset()

        try:
            markets = self.feed.snapshots()
        except FeedError as exc:
            log.error("cycle aborted: %s", exc)
            return self._halted_report(started_at, f"ticker feed unavailable: {exc}")

        # 1. Grade what is already open, against this cycle's prices.
        prices = {m.symbol: m.last for m in markets}
        for update in update_open_signals(self.store, prices, now=started_at):
            self.notifiers.signal_resolved(update)

        # 2. Stage 1 -- cheap filter over every market.
        open_symbols = self.store.open_symbols()
        universe = stage1_universe(markets, self.settings, excluded_symbols=open_symbols)

        # 3. Stage 2 -- one indicator pass each, ranked by how live the setup is.
        candidates: list[Candidate] = []
        features_by_symbol: dict[str, TechnicalFeatures] = {}
        fetch_failures = 0
        stale_charts = 0
        for market in universe:
            candles = self.feed.candles(market.symbol)
            if candles is None:
                fetch_failures += 1
                continue
            if candles_are_stale(candles, self.settings):
                log.warning("%s: candles are stale (%.0fs old), skipping",
                            market.symbol, candles.age_seconds())
                stale_charts += 1
                continue
            features = compute_features(candles)
            if features is None:
                continue
            features_by_symbol[market.symbol] = features
            candidates.append(setup_score(market, features, self.settings))

        self.last_features = features_by_symbol
        finalists = shortlist(candidates, self.settings)

        # 4. Kill-switch, checked before anything fires.
        halted, halt_reason = self._should_halt(fetch_failures, stale_charts, len(universe))

        # 5. Deep analysis and fusion on the shortlist.
        fired = 0
        if not halted:
            fired = self._analyse_and_fire(finalists, features_by_symbol)

        finished_at = utcnow()
        attempts = getattr(stats, "attempts", len(universe) + 1) if stats else len(universe) + 1
        failures = getattr(stats, "failures", fetch_failures) if stats else fetch_failures
        report = ScanReport(
            started_at=started_at, finished_at=finished_at,
            markets_seen=len(markets), passed_stage1=len(universe),
            shortlisted=len(finalists), analysed=len(finalists),
            signals_fired=fired, fetch_failures=failures, fetch_attempts=attempts,
            halted=halted, halt_reason=halt_reason,
            candidates=tuple(finalists),
        )
        self.store.record_cycle(report)
        log.info(
            "cycle: %d markets -> %d universe -> %d shortlist -> %d signals in %.1fs%s",
            report.markets_seen, report.passed_stage1, report.shortlisted,
            report.signals_fired, report.duration_seconds,
            f" [HALTED: {halt_reason}]" if halted else "",
        )
        return report

    def _analyse_and_fire(self, finalists: list[Candidate],
                          features_by_symbol: dict[str, TechnicalFeatures]) -> int:
        capacity = self.settings.max_open_signals - len(self.store.open_symbols())
        if capacity <= 0:
            log.info("at max open signals (%d), holding fire", self.settings.max_open_signals)
            return 0

        pending: list[tuple[float, Candidate, TechnicalFeatures, object]] = []
        for candidate in finalists:
            features = features_by_symbol.get(candidate.symbol)
            if features is None:
                continue
            legs: list[LegScore] = [score_technical(features, self.settings)]
            # Phases 2 and 3 append their legs here; fusion renormalises.
            fusion = fuse(legs, self.settings)
            if fusion.fired:
                pending.append((fusion.confidence, candidate, features, fusion))

        # Best-evidence-first, so a capacity limit drops the weakest calls.
        pending.sort(key=lambda row: row[0], reverse=True)

        fired = 0
        for _, candidate, features, fusion in pending[:capacity]:
            signal = self._build_signal(candidate, features, fusion)
            if signal is None:
                continue
            self.store.insert_signal(signal)
            self.notifiers.signal_fired(signal)
            log.info("FIRED %s %s @ %.0f%% (composite %+.1f)",
                     signal.direction.value.upper(), signal.symbol,
                     signal.confidence, signal.composite_score)
            fired += 1
        return fired

    def _build_signal(self, candidate: Candidate, features: TechnicalFeatures, fusion) -> Signal | None:
        try:
            levels = build_levels(features, fusion.direction, self.settings)
        except ValueError as exc:
            log.warning("skipping %s: %s", candidate.symbol, exc)
            return None

        created_at = utcnow()
        return Signal(
            id=uuid.uuid4().hex[:12],
            symbol=candidate.symbol,
            base=candidate.market.base,
            direction=fusion.direction,
            composite_score=fusion.composite,
            confidence=fusion.confidence,
            levels=levels,
            drivers=fusion.drivers,
            leg_scores=fusion.leg_scores,
            reduced_confidence=fusion.reduced_confidence,
            reduced_confidence_reason=fusion.reduced_confidence_reason,
            reference_price=features.close,
            setup_score=candidate.setup_score,
            created_at=created_at,
            expires_at=expiry_for(created_at, levels.hold_minutes),
        )

    def _should_halt(self, fetch_failures: int, stale_charts: int, attempted: int) -> tuple[bool, str]:
        """The spec's kill-switch: a degraded feed stops new signals.

        A chart that arrived but has stopped updating counts as degraded just
        as much as one that failed to arrive. Counting only hard failures
        would let a venue that has frozen a market keep producing signals off
        a chart that has not moved in an hour.
        """
        if attempted:
            degraded = fetch_failures + stale_charts
            if degraded / attempted > self.settings.max_fetch_failure_ratio:
                parts = []
                if fetch_failures:
                    parts.append(f"{fetch_failures} OHLCV fetch failure(s)")
                if stale_charts:
                    parts.append(f"{stale_charts} stale chart(s)")
                return True, f"{' and '.join(parts)} across {attempted} markets this cycle"

        # Backstop for a feed that keeps answering without ever succeeding.
        stats = getattr(self.feed, "stats", None)
        last_success = getattr(stats, "last_success", None) if stats else None
        if last_success is not None:
            age = time.time() - last_success
            if age > self.settings.stale_feed_seconds:
                return True, f"no successful fetch in {age:.0f}s"
        return False, ""

    def _halted_report(self, started_at: datetime, reason: str) -> ScanReport:
        report = ScanReport(
            started_at=started_at, finished_at=utcnow(),
            markets_seen=0, passed_stage1=0, shortlisted=0, analysed=0,
            signals_fired=0, fetch_failures=1, fetch_attempts=1,
            halted=True, halt_reason=reason,
        )
        self.store.record_cycle(report)
        return report

    # -- the loop ---------------------------------------------------------

    def run_forever(self, max_cycles: int | None = None) -> None:
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            start = time.monotonic()
            try:
                self.run_cycle()
            except Exception:
                log.exception("scan cycle raised; continuing to the next one")
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            # Sleep the remainder of the interval, so a slow cycle does not
            # push every later cycle further off the cadence.
            elapsed = time.monotonic() - start
            time.sleep(max(1.0, self.settings.scan_interval_seconds - elapsed))
