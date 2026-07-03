"""Testnet execution harness: full-lifecycle pass, refusal gates, and
guaranteed cleanup — all against a stateful fake exchange (no SDK import)."""

from __future__ import annotations

import sys
import types

import forven.testnet_harness as th


class FakeExchange:
    """Stateful stand-in for forven.exchange.hyperliquid."""

    def __init__(self, *, baseline_szi=0.0, mid=2000.0, equity=1000.0):
        self.mid = mid
        self.equity = equity
        self.szi = baseline_szi
        self.baseline_szi = baseline_szi
        self.orders: dict[str, dict] = {}
        self._oid = 100
        self.reject_stop = False
        self.no_fill = False
        self.calls: list[str] = []

    # ── module surface ──
    def resolve_configured_testnet(self, default=True):
        self.calls.append("resolve_configured_testnet")
        return True

    def get_account_value(self, testnet=True, require_connection=False, **kw):
        self.calls.append("get_account_value")
        return {"accountValue": self.equity, "source": "exchange"}

    def get_all_mids(self, testnet=True):
        return {"ETH": self.mid}

    def get_positions(self, testnet=True, **kw):
        return {"positions": [{"position": {"coin": "ETH", "szi": self.szi}}]}

    def get_open_orders(self, testnet=True, **kw):
        return [{"coin": "ETH", "oid": oid} for oid in self.orders]

    def market_order(self, asset, side, size, testnet=True, idempotency_key=None, **kw):
        self.calls.append("market_order")
        if self.no_fill:
            return {"entry_price": self.mid, "filled_size": size, "fill_price_unknown": True}
        self._oid += 1
        self.szi += size
        return {
            "entry_price": self.mid,
            "filled_size": size,
            "entry_order_id": str(self._oid),
        }

    def place_protective_stop(self, asset, direction, size, stop_px, testnet=True, **kw):
        self.calls.append("place_protective_stop")
        if self.reject_stop:
            return {"error": "Order would immediately trigger"}
        self._oid += 1
        oid = str(self._oid)
        self.orders[oid] = {"stop_px": stop_px, "size": size}
        return {"stop_order_id": oid, "stop_loss": stop_px}

    def cancel_order(self, asset, oid, testnet=True, **kw):
        self.calls.append("cancel_order")
        self.orders.pop(str(oid), None)
        return {"status": "ok"}

    def close_position(self, asset, size, side="sell", testnet=True, **kw):
        self.calls.append("close_position")
        self.szi -= size
        return {"exit_price": self.mid * 0.999, "filled_size": size}


def _install_module(monkeypatch, module) -> None:
    # Patch BOTH lookup paths: sys.modules covers a first-time import, but once
    # another test has imported the real adapter, `from forven.exchange import
    # hyperliquid` resolves the PACKAGE ATTRIBUTE — patch that too.
    import forven.exchange as exchange_pkg

    monkeypatch.setitem(sys.modules, "forven.exchange.hyperliquid", module)
    monkeypatch.setattr(exchange_pkg, "hyperliquid", module, raising=False)


def _install(monkeypatch, fake: FakeExchange):
    module = types.SimpleNamespace(
        resolve_configured_testnet=fake.resolve_configured_testnet,
        get_account_value=fake.get_account_value,
        get_all_mids=fake.get_all_mids,
        get_positions=fake.get_positions,
        get_open_orders=fake.get_open_orders,
        market_order=fake.market_order,
        place_protective_stop=fake.place_protective_stop,
        cancel_order=fake.cancel_order,
        close_position=fake.close_position,
    )
    _install_module(monkeypatch, module)


def _legs_by_name(report):
    return {leg["leg"]: leg for leg in report["legs"]}


def test_full_lifecycle_passes_and_leaves_no_residue(forven_db, monkeypatch):
    fake = FakeExchange(baseline_szi=0.25)  # pre-existing testnet position — must survive
    _install(monkeypatch, fake)

    report = th.run_testnet_execution_harness()
    legs = _legs_by_name(report)

    assert report["status"] == "passed", report
    for name in ("preflight", "baseline", "open", "stop_mirror", "trailing_tighten", "close", "no_residuals"):
        assert legs[name]["ok"], legs[name]
    # Exchange truth after the run: pre-existing position intact, nothing resting.
    assert abs(fake.szi - 0.25) < 1e-9
    assert fake.orders == {}
    # Report persisted for the ops endpoint / morning check.
    assert th.get_last_harness_report()["status"] == "passed"


def test_refuses_mainnet_without_touching_exchange(forven_db, monkeypatch):
    fake = FakeExchange()

    def _not_testnet(default=True):
        return False

    module = types.SimpleNamespace(resolve_configured_testnet=_not_testnet)
    _install_module(monkeypatch, module)

    report = th.run_testnet_execution_harness()
    assert report["status"] == "skipped"
    assert "mainnet" in report["reason"]
    assert fake.calls == []  # nothing was ever ordered


def test_skips_when_kill_switch_active(forven_db, monkeypatch):
    from forven.db import kv_set

    fake = FakeExchange()
    _install(monkeypatch, fake)
    kv_set("risk_state", {"kill_switch_active": True})

    report = th.run_testnet_execution_harness()
    assert report["status"] == "skipped"
    assert "kill switch" in report["reason"]
    assert "market_order" not in fake.calls


def test_skips_under_sim_clock(forven_db, monkeypatch):
    import forven.sim.clock as sim_clock

    monkeypatch.setattr(sim_clock, "is_sim_active", lambda: True)
    report = th.run_testnet_execution_harness()
    assert report["status"] == "skipped"
    assert "sim" in report["reason"]


def test_stop_rejection_fails_leg_and_cleanup_flattens(forven_db, monkeypatch):
    fake = FakeExchange(baseline_szi=0.0)
    fake.reject_stop = True
    _install(monkeypatch, fake)

    report = th.run_testnet_execution_harness()
    legs = _legs_by_name(report)

    assert report["status"] == "failed"
    assert legs["open"]["ok"]
    assert not legs["stop_mirror"]["ok"]
    assert "trailing_tighten" not in legs  # halted at the failed leg
    # Guaranteed cleanup: the opened delta was flattened, nothing resting.
    assert legs["cleanup"]["ok"], legs["cleanup"]
    assert abs(fake.szi) < 1e-9
    assert fake.orders == {}
    assert "close_position" in fake.calls


def test_unconfirmed_entry_fill_fails_open_leg(forven_db, monkeypatch):
    fake = FakeExchange()
    fake.no_fill = True
    _install(monkeypatch, fake)

    report = th.run_testnet_execution_harness()
    legs = _legs_by_name(report)
    assert report["status"] == "failed"
    assert not legs["open"]["ok"]
    assert "fill" in legs["open"]["detail"]
    assert abs(fake.szi) < 1e-9  # fake never applied a fill


def test_last_report_never_run(forven_db):
    assert th.get_last_harness_report() == {"status": "never_run"}


def test_reconcile_ownership_marker_lifecycle(forven_db, monkeypatch):
    # Semantics the exchange reconciler relies on: fresh+asset-matched -> True;
    # wrong asset / expired / missing -> False (fail closed to orphan handling).
    assert th.harness_position_expected("ETH") is False
    th._mark_harness_active("ETH")
    assert th.harness_position_expected("ETH") is True
    assert th.harness_position_expected("eth") is True  # case-insensitive
    assert th.harness_position_expected("BTC") is False
    monkeypatch.setattr(th.time, "time", lambda: 10**12)  # far future -> expired
    assert th.harness_position_expected("ETH") is False
    monkeypatch.undo()
    th._clear_harness_active()
    assert th.harness_position_expected("ETH") is False


def test_marker_cleared_after_run_even_on_failure(forven_db, monkeypatch):
    fake = FakeExchange()
    fake.reject_stop = True  # forces a failed run through the finally path
    _install(monkeypatch, fake)
    th.run_testnet_execution_harness()
    assert th.harness_position_expected("ETH") is False
