"""Alert formatting and the configuration guard rails."""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_store_and_tracker import make_signal

from cryptosignal.alerts import NotifierGroup, build_notifiers, format_resolution, format_signal
from cryptosignal.alerts.telegram import TelegramNotifier
from cryptosignal.config import Settings
from cryptosignal.models import timeframe_minutes
from cryptosignal.tracker import TrackerUpdate


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "ok"):
        self.status_code = status_code
        self.text = text


class FakeHTTP:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.calls = []

    def post(self, url, json=None):
        self.calls.append((url, json))
        return FakeResponse(self.status_code)


# ---- formatting ------------------------------------------------------------


def test_the_card_carries_every_level_and_the_disclaimer(settings):
    text = format_signal(make_signal(), settings)
    assert "LONG" in text and "BTC/USDT" in text
    for label in ("entry", "stop", "target", "window", "why:"):
        assert label in text
    assert "Not financial advice" in text


def test_the_card_names_the_drivers(settings):
    text = format_signal(make_signal(), settings)
    assert "Trend" in text
    assert "EMA stack aligned" in text


def test_a_flagged_card_says_why(settings):
    signal = make_signal()
    signal.reduced_confidence = True
    signal.reduced_confidence_reason = "technical leg only"
    assert "reduced confidence: technical leg only" in format_signal(signal, settings)


def test_the_holding_window_reads_as_time(settings):
    assert "2h" in format_signal(make_signal(hold_minutes=120), settings)
    assert "45m" in format_signal(make_signal(hold_minutes=45), settings)
    assert "1h30m" in format_signal(make_signal(hold_minutes=90), settings)


def test_a_resolution_reports_the_realised_result():
    signal = make_signal()
    signal.realized_r = -1.0
    text = format_resolution(TrackerUpdate(signal, "stop", 95.0, "stop 95 hit"))
    assert "STOPPED OUT" in text
    assert "-1.00R" in text


def test_a_running_target_one_is_not_reported_as_a_result():
    signal = make_signal()
    text = format_resolution(TrackerUpdate(signal, "target1", 107.5, "target 1 hit"))
    assert "TARGET 1 HIT" in text
    assert "running" in text


# ---- telegram --------------------------------------------------------------


def test_telegram_posts_to_the_bot_api(settings):
    configured = replace(settings, telegram_bot_token="123:abc", telegram_chat_id="42")
    http = FakeHTTP()
    TelegramNotifier(configured, client=http).signal_fired(make_signal())

    url, body = http.calls[0]
    assert url.endswith("/bot123:abc/sendMessage")
    assert body["chat_id"] == "42"
    assert "BTC/USDT" in body["text"]


def test_telegram_escapes_html(settings):
    """A ticker or a driver detail containing < or & must not break the parse."""
    configured = replace(settings, telegram_bot_token="t", telegram_chat_id="c")
    http = FakeHTTP()
    signal = make_signal()
    signal.reduced_confidence = True
    signal.reduced_confidence_reason = "RSI < 30 & falling"
    TelegramNotifier(configured, client=http).signal_fired(signal)

    text = http.calls[0][1]["text"]
    assert "&lt; 30 &amp; falling" in text


def test_telegram_raises_on_an_api_error(settings):
    configured = replace(settings, telegram_bot_token="t", telegram_chat_id="c")
    notifier = TelegramNotifier(configured, client=FakeHTTP(status_code=429))
    with pytest.raises(RuntimeError, match="429"):
        notifier.signal_fired(make_signal())


def test_dry_run_sends_nothing(settings):
    configured = replace(settings, telegram_bot_token="t", telegram_chat_id="c", dry_run=True)
    http = FakeHTTP()
    TelegramNotifier(configured, client=http).signal_fired(make_signal())
    assert http.calls == []


def test_a_broken_channel_does_not_stop_the_others(settings):
    class Broken:
        name = "broken"

        def signal_fired(self, signal):
            raise RuntimeError("down")

        def signal_resolved(self, update):
            raise RuntimeError("down")

    class Working:
        name = "working"

        def __init__(self):
            self.seen = []

        def signal_fired(self, signal):
            self.seen.append(signal.id)

        def signal_resolved(self, update):
            self.seen.append(update.kind)

    working = Working()
    NotifierGroup([Broken(), working]).signal_fired(make_signal())
    assert len(working.seen) == 1


def test_no_credentials_means_no_channels(settings):
    assert build_notifiers(replace(settings, telegram_bot_token="", webhook_url="")).names == []


def test_credentials_enable_the_channels(settings):
    configured = replace(settings, telegram_bot_token="t", telegram_chat_id="c",
                         webhook_url="https://example.invalid/hook")
    assert build_notifiers(configured).names == ["telegram", "webhook"]


def test_a_token_without_a_chat_id_is_not_enough(settings):
    configured = replace(settings, telegram_bot_token="t", telegram_chat_id="")
    assert "telegram" not in build_notifiers(configured).names


# ---- config ----------------------------------------------------------------


def test_leg_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="leg weights"):
        Settings(weight_technical=0.6, weight_fundamental=0.25, weight_sentiment=0.25).validate()


def test_technical_component_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="technical component"):
        Settings(tech_w_trend=0.9).validate()


def test_setup_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="setup score"):
        Settings(setup_w_volatility=0.9).validate()


def test_thresholds_must_straddle_zero():
    with pytest.raises(ValueError, match="short_threshold"):
        Settings(short_threshold=10.0).validate()
    with pytest.raises(ValueError, match="long_threshold"):
        Settings(long_threshold=0.0).validate()


def test_the_holding_band_must_be_ordered():
    with pytest.raises(ValueError, match="min_hold_minutes"):
        Settings(min_hold_minutes=5000, max_hold_minutes=60).validate()


def test_the_shortlist_cannot_exceed_the_universe():
    with pytest.raises(ValueError, match="shortlist_size"):
        Settings(universe_size=5, shortlist_size=10).validate()


def test_targets_must_be_ordered():
    with pytest.raises(ValueError, match="target1_r"):
        Settings(target1_r=3.0, target2_r=2.0).validate()


def test_defaults_are_valid():
    Settings().validate()


def test_environment_overrides_are_read(monkeypatch):
    monkeypatch.setenv("CS_UNIVERSE_SIZE", "42")
    monkeypatch.setenv("CS_EXCLUDED_BASES", "usdt, foo")
    configured = Settings()
    assert configured.universe_size == 42
    assert configured.excluded_bases == ("USDT", "FOO")


def test_a_non_numeric_override_fails_loudly(monkeypatch):
    monkeypatch.setenv("CS_UNIVERSE_SIZE", "lots")
    with pytest.raises(ValueError, match="CS_UNIVERSE_SIZE"):
        Settings()


def test_timeframe_parsing():
    assert timeframe_minutes("15m") == 15
    assert timeframe_minutes("4h") == 240
    assert timeframe_minutes("1d") == 1440
    for bad in ("", "15", "m15", "0m", "15s"):
        with pytest.raises(ValueError):
            timeframe_minutes(bad)
