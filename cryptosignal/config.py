"""Every tunable number in one place.

The spec calls the thresholds "tunable" and the phased plan ends in a
backtesting harness that retunes them. So nothing here is allowed to be a
literal buried in the scoring code -- a weight you cannot find is a weight you
cannot backtest.

Values are read from the environment once, at import of `settings`, so a
running scanner cannot half-apply a change mid-cycle.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields


def _env_str(key: str, default: str) -> str:
    value = os.environ.get(key)
    return default if value is None or value == "" else value


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return tuple(part.strip().upper() for part in raw.split(",") if part.strip())


# Stablecoins and wrapped/staked derivatives. Stage 1 drops these outright: a
# stablecoin has no trend to trade, and a wrapped asset just mirrors its
# underlying, so a signal on both is the same bet counted twice.
DEFAULT_EXCLUDED_BASES = (
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDP", "USDD", "PYUSD",
    "EURT", "EURS", "GUSD", "LUSD", "FRAX", "USDE", "SUSDE", "USD1",
    "WBTC", "WETH", "WBETH", "BETH", "STETH", "WSTETH", "RETH", "CBETH",
    "WBNB", "WSOL", "MSOL", "JITOSOL", "SAVAX", "WMATIC", "WAVAX",
)


@dataclass(frozen=True)
class Settings:
    # ---- exchange / ingestion -------------------------------------------
    exchange_id: str = field(default_factory=lambda: _env_str("CS_EXCHANGE", "binance"))
    quote_currency: str = field(default_factory=lambda: _env_str("CS_QUOTE", "USDT").upper())
    timeframe: str = field(default_factory=lambda: _env_str("CS_TIMEFRAME", "15m"))
    ohlcv_limit: int = field(default_factory=lambda: _env_int("CS_OHLCV_LIMIT", 300))
    # ccxt is told to self-throttle; this is the extra courtesy gap we add on
    # top, in milliseconds, between our own calls.
    request_spacing_ms: int = field(default_factory=lambda: _env_int("CS_REQUEST_SPACING_MS", 120))
    # Tickers move every second but we only need them once per cycle.
    ticker_cache_seconds: float = field(default_factory=lambda: _env_float("CS_TICKER_CACHE_S", 30.0))
    ohlcv_cache_seconds: float = field(default_factory=lambda: _env_float("CS_OHLCV_CACHE_S", 45.0))

    # ---- universe / screening -------------------------------------------
    # Phase 1 of the build plan is a top-20 universe. Raise toward the
    # spec's 200-300 once the on-chain leg lands and the rate budget allows.
    universe_size: int = field(default_factory=lambda: _env_int("CS_UNIVERSE_SIZE", 20))
    min_quote_volume_24h: float = field(default_factory=lambda: _env_float("CS_MIN_VOLUME_24H", 25_000_000.0))
    max_spread_bps: float = field(default_factory=lambda: _env_float("CS_MAX_SPREAD_BPS", 8.0))
    excluded_bases: tuple[str, ...] = field(default_factory=lambda: _env_csv("CS_EXCLUDED_BASES", DEFAULT_EXCLUDED_BASES))
    # Stage 2 hands this many candidates to the deep analysis engine.
    shortlist_size: int = field(default_factory=lambda: _env_int("CS_SHORTLIST_SIZE", 10))

    # ---- setup score (stage 2) ------------------------------------------
    setup_w_volatility: float = field(default_factory=lambda: _env_float("CS_SETUP_W_VOLATILITY", 0.30))
    setup_w_volume: float = field(default_factory=lambda: _env_float("CS_SETUP_W_VOLUME", 0.30))
    setup_w_price_action: float = field(default_factory=lambda: _env_float("CS_SETUP_W_PRICE_ACTION", 0.30))
    setup_w_catalyst: float = field(default_factory=lambda: _env_float("CS_SETUP_W_CATALYST", 0.10))

    # ---- leg weights (fusion) -------------------------------------------
    # The spec's ~50/25/25 split. Phase 1 ships the technical leg only; fusion
    # renormalises over the legs that actually reported, so these stay at their
    # final values and phases 2-3 need no reweighting.
    weight_technical: float = field(default_factory=lambda: _env_float("CS_W_TECHNICAL", 0.50))
    weight_fundamental: float = field(default_factory=lambda: _env_float("CS_W_FUNDAMENTAL", 0.25))
    weight_sentiment: float = field(default_factory=lambda: _env_float("CS_W_SENTIMENT", 0.25))

    # ---- technical leg component weights --------------------------------
    tech_w_trend: float = field(default_factory=lambda: _env_float("CS_TECH_W_TREND", 0.30))
    tech_w_momentum: float = field(default_factory=lambda: _env_float("CS_TECH_W_MOMENTUM", 0.25))
    tech_w_volatility: float = field(default_factory=lambda: _env_float("CS_TECH_W_VOLATILITY", 0.10))
    tech_w_volume: float = field(default_factory=lambda: _env_float("CS_TECH_W_VOLUME", 0.15))
    tech_w_structure: float = field(default_factory=lambda: _env_float("CS_TECH_W_STRUCTURE", 0.20))

    # ---- signal thresholds ----------------------------------------------
    long_threshold: float = field(default_factory=lambda: _env_float("CS_LONG_THRESHOLD", 60.0))
    short_threshold: float = field(default_factory=lambda: _env_float("CS_SHORT_THRESHOLD", -60.0))
    # Confidence at the threshold, and at a saturated |100| score.
    confidence_floor: float = field(default_factory=lambda: _env_float("CS_CONFIDENCE_FLOOR", 55.0))
    confidence_ceiling: float = field(default_factory=lambda: _env_float("CS_CONFIDENCE_CEILING", 95.0))
    # Applied when the legs point opposite ways: fire, but say so.
    disagreement_penalty: float = field(default_factory=lambda: _env_float("CS_DISAGREEMENT_PENALTY", 0.80))

    # ---- levels ----------------------------------------------------------
    entry_band_atr: float = field(default_factory=lambda: _env_float("CS_ENTRY_BAND_ATR", 0.25))
    stop_atr_multiple: float = field(default_factory=lambda: _env_float("CS_STOP_ATR", 1.5))
    target1_r: float = field(default_factory=lambda: _env_float("CS_TARGET1_R", 1.5))
    target2_r: float = field(default_factory=lambda: _env_float("CS_TARGET2_R", 2.5))
    # The spec's capture window: 15 minutes to 72 hours.
    min_hold_minutes: int = field(default_factory=lambda: _env_int("CS_MIN_HOLD_MINUTES", 15))
    max_hold_minutes: int = field(default_factory=lambda: _env_int("CS_MAX_HOLD_MINUTES", 72 * 60))

    # ---- scanner ---------------------------------------------------------
    scan_interval_seconds: float = field(default_factory=lambda: _env_float("CS_SCAN_INTERVAL_S", 120.0))
    # One open signal per coin at a time; stage 1 filters the rest out.
    max_open_signals: int = field(default_factory=lambda: _env_int("CS_MAX_OPEN_SIGNALS", 8))
    # Kill-switch: halt new signals when the feed has not produced a clean
    # cycle within this many seconds, or when a cycle's fetch failure rate
    # exceeds the ratio below.
    stale_feed_seconds: float = field(default_factory=lambda: _env_float("CS_STALE_FEED_S", 600.0))
    max_fetch_failure_ratio: float = field(default_factory=lambda: _env_float("CS_MAX_FETCH_FAILURE_RATIO", 0.34))
    # A candle whose close is older than this many multiples of the timeframe
    # means the exchange stopped publishing; that coin is skipped.
    max_candle_age_multiple: float = field(default_factory=lambda: _env_float("CS_MAX_CANDLE_AGE_MULT", 3.0))

    # ---- delivery --------------------------------------------------------
    database_path: str = field(default_factory=lambda: _env_str("CS_DB_PATH", "cryptosignal.db"))
    telegram_bot_token: str = field(default_factory=lambda: _env_str("CS_TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _env_str("CS_TELEGRAM_CHAT_ID", ""))
    webhook_url: str = field(default_factory=lambda: _env_str("CS_WEBHOOK_URL", ""))
    api_host: str = field(default_factory=lambda: _env_str("CS_API_HOST", "127.0.0.1"))
    api_port: int = field(default_factory=lambda: _env_int("CS_API_PORT", 8000))
    dry_run: bool = field(default_factory=lambda: _env_bool("CS_DRY_RUN", False))

    DISCLAIMER: str = (
        "Not financial advice. Signals are decision support, not an auto-trader. "
        "Short-horizon crypto signals carry a high false-positive rate."
    )

    def validate(self) -> None:
        """Fail at startup rather than producing quietly wrong scores."""
        leg_total = self.weight_technical + self.weight_fundamental + self.weight_sentiment
        if abs(leg_total - 1.0) > 1e-6:
            raise ValueError(f"leg weights must sum to 1.0, got {leg_total:.4f}")

        tech_total = (
            self.tech_w_trend + self.tech_w_momentum + self.tech_w_volatility
            + self.tech_w_volume + self.tech_w_structure
        )
        if abs(tech_total - 1.0) > 1e-6:
            raise ValueError(f"technical component weights must sum to 1.0, got {tech_total:.4f}")

        setup_total = (
            self.setup_w_volatility + self.setup_w_volume
            + self.setup_w_price_action + self.setup_w_catalyst
        )
        if abs(setup_total - 1.0) > 1e-6:
            raise ValueError(f"setup score weights must sum to 1.0, got {setup_total:.4f}")

        if not 0 < self.long_threshold <= 100:
            raise ValueError("long_threshold must be in (0, 100]")
        if not -100 <= self.short_threshold < 0:
            raise ValueError("short_threshold must be in [-100, 0)")
        if self.confidence_floor >= self.confidence_ceiling:
            raise ValueError("confidence_floor must be below confidence_ceiling")
        if self.min_hold_minutes >= self.max_hold_minutes:
            raise ValueError("min_hold_minutes must be below max_hold_minutes")
        if self.shortlist_size > self.universe_size:
            raise ValueError("shortlist_size cannot exceed universe_size")
        if self.target1_r >= self.target2_r:
            raise ValueError("target1_r must be below target2_r")
        if self.stop_atr_multiple <= 0:
            raise ValueError("stop_atr_multiple must be positive")

    def as_dict(self) -> dict[str, object]:
        """Public view of the tuning, for the dashboard's config panel."""
        out: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if "token" in f.name or "chat_id" in f.name or "webhook" in f.name:
                out[f.name] = "set" if value else "unset"
            elif isinstance(value, tuple):
                out[f.name] = list(value)
            else:
                out[f.name] = value
        return out


settings = Settings()
settings.validate()
