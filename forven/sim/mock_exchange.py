"""Mock exchange for simulation mode — fills against virtual prices.

Intercepts exchange calls to provide a virtual environment.
"""

import logging
import uuid
from forven.db import kv_get, get_db
from forven.sim.clock import sim_kv_key, get_now

log = logging.getLogger("forven.sim.exchange")

def _get_virtual_price(asset: str) -> float:
    """Get the current virtual price from market cache."""
    # During sim, SimulationRunner publishes to sim_kv_key('market:prices')
    snapshot = kv_get(sim_kv_key("market:prices"), {})
    prices = snapshot.get("prices", {})
    return float(prices.get(asset.upper(), 0))

def _calculate_dynamic_slippage(asset: str, is_buy: bool, is_close: bool = False) -> float:
    """Calculate dynamic slippage based on recent volatility (ATR/Spread)."""
    base_bps = 3.0 if is_close else 2.0  # 3 bps for close, 2 bps for open
    try:
        from forven.sim.data_pump import get_cached_candles
        end_ms = int(get_now().timestamp() * 1000)
        # We need the interval the simulation is running at. Default to 1h if unknown.
        interval = kv_get("simulation_state", {}).get("interval", "1h")
        df = get_cached_candles(asset, interval, end_ms, bars=1)
        if df is not None and not df.empty:
            c = df.iloc[-1]
            high, low, close = float(c["high"]), float(c["low"]), float(c["close"])
            if close > 0:
                spread_pct = (high - low) / close
                # Add a portion of the candle's spread as slippage penalty (e.g., 5% of spread)
                volatility_penalty_bps = (spread_pct * 0.05) * 10000
                base_bps += min(volatility_penalty_bps, 50.0) # Cap penalty at 50 bps
    except Exception as e:
        log.debug("Failed to calculate dynamic slippage for %s: %s", asset, e)
    
    slippage_multiplier = 1.0 + (base_bps / 10000.0) if is_buy else 1.0 - (base_bps / 10000.0)
    return slippage_multiplier

def sim_market_order(asset, side, size, stop_loss_price=None, take_profit_price=None) -> dict:
    """Simulate a market order fill against virtual prices."""
    mid = _get_virtual_price(asset)
    if mid <= 0:
        return {"error": f"No virtual price for {asset}"}

    is_buy = side.upper() in ("B", "BUY", "LONG")
    slippage = _calculate_dynamic_slippage(asset, is_buy, is_close=False)
    fill_price = mid * slippage
    order_id = f"sim-{str(uuid.uuid4())[:8]}"
    stop_order_id = f"sim-stop-{str(uuid.uuid4())[:8]}" if stop_loss_price else None
    take_profit_order_id = f"sim-tp-{str(uuid.uuid4())[:8]}" if take_profit_price else None

    log.info("SIM FILL: %s %s %.4f @ $%.2f (mid=$%.2f)",
             "BUY" if is_buy else "SELL", asset, size, fill_price, mid)

    return {
        "status": "ok",
        "response": {
            "type": "order", 
            "data": {
                "statuses": [
                    {"filled": {"totalSz": str(size), "avgPx": str(fill_price)}}
                ]
            }
        },
        "mid": mid,
        "entry_price": fill_price,
        "order_id": order_id,
        "entry_order_id": order_id,
        "stop_order_id": stop_order_id,
        "take_profit_order_id": take_profit_order_id,
        "order_ids": {
            **({"entry": order_id} if order_id else {}),
            **({"stop": stop_order_id} if stop_order_id else {}),
            **({"take_profit": take_profit_order_id} if take_profit_order_id else {}),
        },
        "stop_loss": stop_loss_price,
        "take_profit": take_profit_price,
    }

def sim_close_position(asset, size, side="sell") -> dict:
    """Simulate closing a position."""
    mid = _get_virtual_price(asset)
    if mid <= 0:
        return {"error": f"No virtual price for {asset}"}

    is_buy = side.lower() in ("b", "buy")
    slippage = _calculate_dynamic_slippage(asset, is_buy, is_close=True)
    close_price = mid * slippage
    order_id = f"sim-close-{str(uuid.uuid4())[:8]}"

    log.info("SIM CLOSE: %s %s %.4f @ $%.2f", side.upper(), asset, size, close_price)

    return {
        "status": "ok",
        "response": {
            "type": "order", 
            "data": {
                "statuses": [
                    {"filled": {"totalSz": str(size), "avgPx": str(close_price)}}
                ]
            }
        },
        "mid": mid,
        "close_price": close_price,
        "order_id": order_id,
    }


def sim_get_positions() -> dict:
    """Return virtual positions derived from open simulation trades in DB."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE execution_type='simulation' AND status='OPEN'"
        ).fetchall()

    positions = []
    for r in rows:
        r = dict(r)
        positions.append({
            "position": {
                "coin": r.get("asset", ""),
                "szi": str(r.get("size", 0)),
                "entryPx": str(r.get("entry_price", 0)),
                "leverage": {"type": "isolated", "value": int(r.get("leverage", 1))},
            }
        })

    return {"positions": positions, "marginSummary": sim_get_account_value()}

def sim_get_account_value() -> dict:
    """Return virtual account state from simulation equity state."""
    state = kv_get("simulation_state", {})
    equity = state.get("equity", 10000.0)
    return {
        "accountValue": equity,
        "totalMarginUsed": 0,
        "totalNtlPos": 0,
        "totalRawUsd": equity,
        # PAPER-HALT-2: without a label the daemon defaulted this to "exchange",
        # so SIMULATED losses read as a real-capital basis and could arm the
        # real kill-switch. "sim" is a non-real basis: metrics only, no halts.
        "source": "sim",
    }

def sim_get_all_mids() -> dict[str, float]:
    """Return all virtual prices currently in the market cache."""
    snapshot = kv_get("market:prices", {})
    prices = snapshot.get("prices", {})
    return {str(k).upper(): float(v) for k, v in prices.items() if float(v) > 0}
