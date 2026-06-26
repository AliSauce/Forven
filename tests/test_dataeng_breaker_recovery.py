"""Data-source circuit-breaker recovery + benign-empty handling.

Two regressions that left OI/funding stuck "Down" for every symbol:

1. An empty incremental window (NoData) -- the NORMAL case when we poll OI/funding
   more often than new bars close, e.g. ADA 4h between 4h boundaries, or a symbol
   the exchange simply has no OI for -- was recorded as a circuit-breaker FAILURE.
   With 26 symbols x several timeframes, >=3 empty windows happened every cycle, so
   the shared "binance" breaker tripped every run and latched OI+funding off for ALL
   symbols.
2. Once open, the breaker had no recovery path: ``resolve`` refused any open source,
   but a breaker only closes on a successful fetch it would never be allowed to make
   -- so it stayed open until the process restarted.
"""
from __future__ import annotations

import pytest


# --------------------------------------------------------------------------- #
# CircuitBreaker half-open recovery
# --------------------------------------------------------------------------- #
def test_breaker_opens_then_half_opens_after_cooldown_and_closes():
    from forven.dataeng.source import CircuitBreaker

    b = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=0.0)
    b.record_failure()
    b.record_failure()
    b.record_failure()
    assert b.status == "open"
    # cooldown elapsed (0s) -> one half-open trial is allowed
    assert b.allow_request() is True
    assert b.status == "half_open"
    b.record_success()
    assert b.status == "closed"


def test_breaker_stays_open_within_cooldown():
    from forven.dataeng.source import CircuitBreaker

    b = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=10_000.0)
    b.record_failure()
    b.record_failure()
    assert b.status == "open"
    assert b.allow_request() is False  # cooldown not elapsed -> still latched
    assert b.status == "open"


def test_half_open_trial_failure_reopens_and_resets_cooldown():
    from forven.dataeng.source import CircuitBreaker

    b = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=0.0)
    b.record_failure()
    b.record_failure()
    assert b.allow_request() is True and b.status == "half_open"
    b.record_failure()  # the trial itself failed
    assert b.status == "open"


# --------------------------------------------------------------------------- #
# _fetch_stream_via_source_registry: NoData is benign, real errors trip
# --------------------------------------------------------------------------- #
def _fake_registry_with(source):
    from forven.dataeng.source import SourceRegistry

    reg = SourceRegistry()
    reg.register(source)
    return reg


def test_registry_fetch_treats_nodata_as_empty_not_a_failure(monkeypatch):
    from forven.dataeng.errors import NoData
    from forven.dataeng.source import Stream
    import forven.data_manager as dm

    class _EmptySource:
        id = "binance"
        capabilities = {Stream.OI, Stream.FUNDING, Stream.CANDLES}

        def fetch(self, ref, stream, since=None, until=None):
            raise NoData("no open interest in window")

    reg = _fake_registry_with(_EmptySource())
    monkeypatch.setattr("forven.dataeng.source.get_source_registry", lambda: reg)

    out = dm._fetch_stream_via_source_registry("ADA-USDT", "oi", timeframe="4h", since=123)
    assert out is not None and out.empty            # benign empty -> 0 rows saved
    health = reg.health("binance")
    assert health.status == "closed"                 # breaker NOT tripped
    assert health.consecutive_failures == 0


def test_registry_fetch_real_error_does_trip_breaker(monkeypatch):
    from forven.dataeng.source import Stream
    import forven.data_manager as dm

    class _BadSource:
        id = "binance"
        capabilities = {Stream.OI}

        def fetch(self, ref, stream, since=None, until=None):
            raise RuntimeError("genuine source failure")

    reg = _fake_registry_with(_BadSource())
    monkeypatch.setattr("forven.dataeng.source.get_source_registry", lambda: reg)

    for _ in range(3):
        with pytest.raises(RuntimeError):
            dm._fetch_stream_via_source_registry("ADA-USDT", "oi", timeframe="4h")
    assert reg.health("binance").status == "open"    # real failures still latch it
