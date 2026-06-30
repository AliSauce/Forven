"""The daemon must publish the price snapshot BEFORE the (possibly slow) risk cycle.

publish_price_snapshot stamps updated_at at publish time, so publishing at the END of a slow
tick stamped a now-stale price as fresh — a lag the consumers' 120s age gate can't see (it
filled a paper short ~$80 off the candle). This pins the ordering so it can't silently regress.
"""

from __future__ import annotations

import pytest

import forven.daemon as d


@pytest.mark.asyncio
async def test_run_tick_publishes_price_before_risk_cycle(forven_db, monkeypatch):
    order: list[str] = []

    async def fake_risk_cycle():
        order.append("risk_cycle")
        return {}

    def fake_publish(prices, source):
        order.append("publish")
        return {"prices": prices}

    async def fake_to_thread(name, timeout, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(d, "_run_risk_cycle", fake_risk_cycle)
    monkeypatch.setattr(d, "publish_price_snapshot", fake_publish)
    monkeypatch.setattr(d, "_to_thread_with_timeout", fake_to_thread)
    monkeypatch.setattr(d, "_LAST_LIQ_CHECK", [float("inf")])  # liquidation gate not due -> skipped

    state: dict = {}
    await d._run_tick(state, {"BTC": 100.0}, "ws", [float("inf")])  # reconcile gate not due

    assert "publish" in order and "risk_cycle" in order
    assert order.index("publish") < order.index("risk_cycle"), f"publish must precede risk cycle: {order}"
    assert state["last_prices"] == {"BTC": 100.0}  # the fresh prices were published
    assert state["last_price_source"] == "ws"
