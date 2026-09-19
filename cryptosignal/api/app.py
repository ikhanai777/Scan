"""The dashboard API.

Read-only by design: v1 generates and delivers signals, it does not take
orders. Every signal payload carries the disclaimer, because a card that gets
scraped out of the JSON and pasted somewhere else should carry it too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from ..config import Settings
from ..config import settings as default_settings
from ..models import utcnow
from ..scanner import Scanner
from ..store import Store

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
# How often the SSE stream re-reads the database looking for something new.
STREAM_POLL_SECONDS = 2.0
STREAM_HEARTBEAT_SECONDS = 20.0


def create_app(settings: Settings | None = None, store: Store | None = None,
               scanner: Scanner | None = None) -> FastAPI:
    settings = settings or default_settings
    store = store or Store(settings.database_path)

    app = FastAPI(
        title="cryptosignal",
        version="0.1.0",
        description="Crypto market scanner and signal feed. Not financial advice.",
    )
    app.state.settings = settings
    app.state.store = store
    app.state.scanner = scanner
    app.state.started_at = utcnow()

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health")
    def health() -> dict:
        cycle = store.last_cycle()
        healthy = bool(cycle) and not cycle.get("halted", False)
        return {
            "status": "ok" if healthy else "degraded",
            "uptime_seconds": round((utcnow() - app.state.started_at).total_seconds(), 1),
            "last_cycle_at": cycle.get("finished_at") if cycle else None,
            "halted": cycle.get("halted", False) if cycle else None,
            "halt_reason": cycle.get("halt_reason", "") if cycle else "",
            "scanner_attached": scanner is not None,
        }

    @app.get("/api/signals")
    def list_signals(
        status: str | None = Query(None, description="open | closed | a specific status"),
        direction: str | None = Query(None, pattern="^(long|short)$"),
        symbol: str | None = None,
        min_confidence: float | None = Query(None, ge=0, le=100),
        limit: int = Query(50, ge=1, le=500),
    ) -> dict:
        signals = store.recent_signals(
            limit=limit, status=status, direction=direction,
            symbol=symbol, min_confidence=min_confidence,
        )
        now = utcnow()
        payload = []
        for signal in signals:
            card = signal.to_public_dict()
            card["minutes_remaining"] = round(signal.minutes_remaining(now), 1) if signal.is_open else 0.0
            payload.append(card)
        return {"signals": payload, "count": len(payload), "disclaimer": settings.DISCLAIMER}

    @app.get("/api/signals/{signal_id}")
    def get_signal(signal_id: str) -> dict:
        signal = store.get_signal(signal_id)
        if signal is None:
            raise HTTPException(status_code=404, detail="no such signal")
        card = signal.to_public_dict()
        card["minutes_remaining"] = round(signal.minutes_remaining(), 1) if signal.is_open else 0.0
        card["all_drivers"] = [
            {"label": d.label, "score": round(d.score, 1), "weight": d.weight, "detail": d.detail}
            for d in signal.drivers
        ]
        return {"signal": card, "events": store.events_for(signal_id), "disclaimer": settings.DISCLAIMER}

    @app.get("/api/candidates")
    def candidates() -> dict:
        """What the screen is watching right now, ranked by setup score."""
        cycle = store.last_cycle() or {}
        return {"shortlist": cycle.get("shortlist", []), "as_of": cycle.get("finished_at")}

    @app.get("/api/performance")
    def performance() -> dict:
        return {
            "performance": store.performance(),
            "equity_curve": store.equity_curve(),
            "disclaimer": settings.DISCLAIMER,
        }

    @app.get("/api/status")
    def status() -> dict:
        return {
            "cycle": store.last_cycle(),
            "config": settings.as_dict(),
            "disclaimer": settings.DISCLAIMER,
        }

    @app.get("/api/features/{symbol:path}")
    def features(symbol: str) -> dict:
        """The indicator readings behind a coin's score, for the detail view."""
        if scanner is None:
            raise HTTPException(status_code=503, detail="no scanner attached to this process")
        found = scanner.last_features.get(symbol) or scanner.last_features.get(symbol.upper())
        if found is None:
            raise HTTPException(status_code=404, detail="symbol not in the last scan cycle")
        return {"symbol": symbol, "features": _feature_dict(found)}

    @app.get("/api/execution")
    def execution() -> dict:
        """Order state and the risk limits, or an explicit 'off'.

        Always answers rather than 404ing when execution is disabled: a panel
        that cannot tell "off" from "broken" is worse than useless on the one
        screen where that distinction matters most.
        """
        if scanner is None:
            # Not the same as disabled: the scanner is running in another
            # process, so this one has nothing to report either way.
            return {"enabled": False, "attached": False,
                    "reason": "no scanner in this process -- start with `serve --scan`"}
        engine = scanner.execution
        if engine is None:
            return {"enabled": False, "attached": True,
                    "reason": f"execution is off (CS_EXECUTION_MODE={settings.execution_mode})"}
        return {"attached": True, **engine.snapshot()}

    @app.get("/api/sources")
    def sources() -> dict:
        """Which data providers actually answered on the last cycle."""
        legs = {
            "fundamental": settings.enable_fundamental_leg,
            "sentiment": settings.enable_sentiment_leg,
        }
        if scanner is None:
            return {"attached": False, "reporting": {}, "feeds": {}, "regime": None, "legs": legs}
        context = scanner.context
        if context is None:
            return {"attached": True, "reporting": {}, "feeds": {}, "regime": None, "legs": legs}
        return {
            "attached": True,
            "reporting": context.sources_reporting,
            "feeds": getattr(context.news, "feed_health", {}) if context.news else {},
            "regime": ({"value": context.regime.value, "label": context.regime.label}
                       if context.regime else None),
            "legs": legs,
        }

    @app.get("/api/history/{symbol:path}")
    def history(symbol: str) -> dict:
        """Recent closes for a symbol, for the card's sparkline."""
        if scanner is None:
            raise HTTPException(status_code=503, detail="no scanner attached to this process")
        tail = getattr(scanner, "last_closes", {}) or {}
        rows = tail.get(symbol) or tail.get(symbol.upper())
        if not rows:
            raise HTTPException(status_code=404, detail="symbol not in the last scan cycle")
        return {"symbol": symbol, "closes": rows}

    @app.get("/api/risk")
    def risk() -> dict:
        """How concentrated the open book really is."""
        if scanner is None:
            return {"attached": False}
        heat = getattr(scanner, "heat", None)
        snapshot = heat.snapshot(settings.correlation_bars) if heat else {}
        return {
            "attached": True,
            "enabled": settings.enable_correlation_control,
            **snapshot,
            "limits": {
                "max_pair_correlation": settings.max_pair_correlation,
                "max_effective_exposure": settings.max_effective_exposure,
            },
        }

    @app.get("/api/regimes")
    def regimes() -> dict:
        """What kind of market each scanned coin is in right now."""
        if scanner is None:
            return {"attached": False, "regimes": {}}
        readings = getattr(scanner, "last_regimes", {}) or {}
        return {
            "attached": True,
            "regimes": {
                symbol: reading.to_dict()
                for symbol, reading in readings.items() if reading is not None
            },
        }

    @app.get("/api/stream")
    async def stream() -> StreamingResponse:
        return StreamingResponse(_events(store, settings), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


def _feature_dict(features) -> dict:
    import math
    from dataclasses import asdict

    raw = asdict(features)
    # JSON has no nan. A reading that never warmed up is null, not zero --
    # the dashboard draws those as "--" rather than as a real value.
    return {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in raw.items()}


async def _events(store: Store, settings: Settings):
    """Server-sent events: new signals, resolutions, and cycle ticks.

    Polling the database beats an in-process queue here because the scanner may
    be a separate process; the dashboard gets the same view either way.
    """
    seen_signal_ids: set[str] = set()
    last_cycle_at: str | None = None
    last_statuses: dict[str, str] = {}
    quiet_for = 0.0

    # Prime with what already exists, so a reconnect does not replay history.
    for signal in store.recent_signals(limit=100):
        seen_signal_ids.add(signal.id)
        last_statuses[signal.id] = signal.status.value

    yield _sse("hello", {"disclaimer": settings.DISCLAIMER, "at": utcnow().isoformat()})

    while True:
        await asyncio.sleep(STREAM_POLL_SECONDS)
        quiet_for += STREAM_POLL_SECONDS
        try:
            recent = store.recent_signals(limit=50)
        except Exception:
            log.exception("stream query failed")
            continue

        for signal in reversed(recent):
            card = signal.to_public_dict()
            if signal.id not in seen_signal_ids:
                seen_signal_ids.add(signal.id)
                last_statuses[signal.id] = signal.status.value
                quiet_for = 0.0
                yield _sse("signal", card)
            elif last_statuses.get(signal.id) != signal.status.value:
                last_statuses[signal.id] = signal.status.value
                quiet_for = 0.0
                yield _sse("resolution", card)

        cycle = store.last_cycle()
        if cycle and cycle.get("finished_at") != last_cycle_at:
            last_cycle_at = cycle.get("finished_at")
            quiet_for = 0.0
            yield _sse("cycle", cycle)

        if quiet_for >= STREAM_HEARTBEAT_SECONDS:
            quiet_for = 0.0
            yield ": keepalive\n\n"


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def run_scanner_in_background(scanner: Scanner) -> threading.Thread:
    """Run the scan loop beside the API, for a single-process deployment."""
    thread = threading.Thread(target=scanner.run_forever, name="scanner", daemon=True)
    thread.start()
    return thread
