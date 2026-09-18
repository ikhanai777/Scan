"""A real end-to-end preflight against the configured exchange.

`cryptosignal doctor` exists because "it runs" and "it is reading a live market"
are different claims, and only the second one matters for a signal app. Every
line this prints is a measurement taken just now against the venue -- a market
count, a real price, the age of the newest candle, the indicator readings off
that candle. Nothing here is simulated, and nothing here is cached from a
previous run.

When a check fails it reports the reason and the specific change that fixes it,
because the first failure most people hit is geographic, not a bug.
"""

from __future__ import annotations

import textwrap
import time
from dataclasses import dataclass, field

from .config import Settings
from .exchange import CCXTFeed, FeedError, candles_are_stale
from .features import MIN_BARS, compute_features
from .fusion import fuse
from .legs import score_technical
from .screen import setup_score, stage1_universe
from .store import Store

PASS, FAIL, WARN, SKIP = "ok", "fail", "warn", "skip"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, fix: str = "") -> Check:
        check = Check(name, status, detail, fix)
        self.checks.append(check)
        return check

    @property
    def ok(self) -> bool:
        return not any(c.failed for c in self.checks)

    def render(self, width: int = 96) -> str:
        glyph = {PASS: "[ok]  ", FAIL: "[FAIL]", WARN: "[warn]", SKIP: "[--]  "}
        indent = " " * 24
        lines = []
        for check in self.checks:
            lines.append(f"  {glyph[check.status]} {check.name:<14} {check.detail}")
            if check.fix:
                # Wrapped, not split on punctuation: an em-dash inside a
                # sentence is not a line break, and cutting there produced
                # fragments that read like two broken half-sentences.
                lines.extend(textwrap.wrap(
                    check.fix, width=width, initial_indent=indent, subsequent_indent=indent,
                ))
        return "\n".join(lines)


def diagnose(settings: Settings, feed: CCXTFeed | None = None) -> Report:
    """Walk the whole pipeline once, against live data, and report each stage."""
    report = Report()

    # -- 1. can we build a client for this venue at all --------------------
    try:
        feed = feed or CCXTFeed(settings)
    except FeedError as exc:
        report.add("exchange", FAIL, str(exc),
                   "CS_EXCHANGE must name a ccxt exchange -- try binance, kraken or coinbase")
        return report

    # -- 2. does it answer, and with how many markets ----------------------
    started = time.monotonic()
    try:
        snapshots = feed.snapshots()
    except FeedError as exc:
        report.add("exchange", FAIL, f"{settings.exchange_id} did not answer", str(exc))
        return report
    elapsed_ms = (time.monotonic() - started) * 1000

    report.add("exchange", PASS, f"{settings.exchange_id} answered in {elapsed_ms:.0f}ms")
    quoted = [s for s in snapshots if s.quote == settings.quote_currency]
    report.add("markets", PASS,
               f"{len(snapshots):,} live spot markets, {len(quoted):,} against {settings.quote_currency}")
    if not quoted:
        report.checks[-1].status = FAIL
        report.checks[-1].fix = (
            f"{settings.exchange_id} lists nothing against {settings.quote_currency}. "
            f"Set CS_QUOTE to a quote this venue actually uses (USD on Kraken and Coinbase, "
            f"USDT on most others)."
        )
        return report

    # -- 3. is the configured candle size available here -------------------
    available = feed.supported_timeframes()
    if available and settings.timeframe not in available:
        report.add("timeframe", FAIL, f"{settings.exchange_id} does not serve {settings.timeframe} candles",
                   f"Set CS_TIMEFRAME to one of: {', '.join(available[:14])}")
        return report
    report.add("timeframe", PASS, f"{settings.timeframe} candles supported")

    # -- 4. a real price, so the numbers are visibly not placeholders ------
    leader = max(quoted, key=lambda s: s.quote_volume_24h)
    spread = f"{leader.spread_bps:.2f}bps spread" if leader.spread_bps == leader.spread_bps else "spread not published"
    report.add("live price", PASS,
               f"{leader.symbol} {leader.last:,.6g}  "
               f"{leader.quote_volume_24h / 1e6:,.0f}M 24h volume, {spread}")

    # -- 5. how many survive the liquidity filter --------------------------
    universe = stage1_universe(snapshots, settings)
    status = PASS if universe else FAIL
    check = report.add("stage 1", status,
                       f"{len(universe)} of {len(quoted):,} markets clear the floor "
                       f"(>{settings.min_quote_volume_24h / 1e6:,.0f}M volume, "
                       f"<{settings.max_spread_bps:g}bps spread)")
    if not universe:
        check.fix = ("Nothing passed. Lower CS_MIN_VOLUME_24H or raise CS_MAX_SPREAD_BPS -- "
                     "a venue smaller than Binance needs a lower floor.")
        return report

    # -- 6. real candles for the most liquid survivor ----------------------
    probe = universe[0]
    candles = feed.candles(probe.symbol)
    if candles is None:
        report.add("candles", FAIL, f"no OHLCV came back for {probe.symbol}",
                   "The ticker endpoint works but the candle endpoint does not. "
                   "Check CS_TIMEFRAME, and whether this venue rate-limits OHLCV harder.")
        return report

    age = candles.age_seconds()
    stale = candles_are_stale(candles, settings)
    report.add("candles", WARN if stale else PASS,
               f"{probe.symbol}: {len(candles)} bars, newest opened {age / 60:.1f} min ago",
               "The newest candle is older than this timeframe should allow -- the scanner "
               "will skip this market rather than score a frozen chart." if stale else "")

    if len(candles) < MIN_BARS:
        report.add("indicators", FAIL, f"only {len(candles)} bars, need {MIN_BARS}",
                   "Raise CS_OHLCV_LIMIT, or this venue caps history on this pair.")
        return report

    # -- 7. the indicators, computed off those real candles ----------------
    features = compute_features(candles)
    if features is None:
        report.add("indicators", FAIL, "the feature pass returned nothing")
        return report
    report.add("indicators", PASS,
               f"ATR {features.atr_pct:.2f}% of price, RSI {features.rsi:.0f}, "
               f"ADX {features.adx:.0f}, rel volume {features.rvol:.2f}x")

    # -- 8. the full scoring path, on that live chart ----------------------
    candidate = setup_score(probe, features, settings)
    leg = score_technical(features, settings)
    fusion = fuse([leg], settings)
    call = fusion.direction.value.upper() if fusion.fired else "no signal"
    report.add("scoring", PASS,
               f"{probe.symbol}: setup {candidate.setup_score:.0f}, "
               f"technical {leg.score:+.0f}, composite {fusion.composite:+.0f} -> {call}")

    # -- 9. can a full cycle stay inside the venue's rate limit ------------
    calls = 1 + settings.universe_size
    venue_gap = feed.rate_limit_ms
    our_gap = max(venue_gap, settings.request_spacing_ms)
    cycle_seconds = calls * our_gap / 1000.0
    fits = cycle_seconds < settings.scan_interval_seconds
    report.add("rate budget", PASS if fits else WARN,
               f"{calls} calls per cycle, ~{cycle_seconds:.0f}s at {our_gap:.0f}ms spacing, "
               f"cycle every {settings.scan_interval_seconds:.0f}s",
               "" if fits else
               "A cycle cannot finish inside its own interval. Lower CS_UNIVERSE_SIZE or "
               "raise CS_SCAN_INTERVAL_S, or cycles will run back to back.")

    # -- 10. where the output goes ----------------------------------------
    channels = []
    if settings.telegram_bot_token and settings.telegram_chat_id:
        channels.append("telegram")
    if settings.webhook_url:
        channels.append("webhook")
    report.add("alerts", PASS if channels else WARN,
               ", ".join(channels) if channels else "no alert channel configured",
               "" if channels else
               "Signals will still be scored, stored and shown on the dashboard. "
               "Set CS_TELEGRAM_BOT_TOKEN and CS_TELEGRAM_CHAT_ID to get pushed.")

    store = Store(settings.database_path)
    try:
        performance = store.performance()
        report.add("database", PASS,
                   f"{settings.database_path}: {performance['open']} open, "
                   f"{performance['closed']} closed signals")
    finally:
        store.close()

    return report
