"""CCXT-backed source adapter."""

from __future__ import annotations

from typing import AsyncIterator

import pandas as pd

from forven.dataeng.errors import NoData, SourceError
from forven.dataeng.identity import SymbolRef, to_ref
from forven.dataeng.source import SourceHealth, Stream


class CcxtSource:
    def __init__(self, exchange_id: str = "binance", exchange: object | None = None) -> None:
        self.id = str(exchange_id or "binance").strip().lower()
        self.exchange = exchange
        # Only what fetch() actually implements — advertising TRADES/ORDERBOOK
        # here made the registry resolve this source for streams it then
        # rejected with SourceError.
        self.capabilities = {Stream.CANDLES, Stream.FUNDING, Stream.OI}
        self._last_error = ""

    def fetch(
        self,
        ref: str | SymbolRef,
        stream: Stream,
        since: object | None = None,
        until: object | None = None,
    ) -> pd.DataFrame:
        resolved = ref if isinstance(ref, SymbolRef) else to_ref(ref, source=self.id)
        try:
            if stream == Stream.CANDLES:
                return self._fetch_candles(resolved, since)
            if stream == Stream.FUNDING:
                return self._fetch_funding(resolved, since)
            if stream == Stream.OI:
                return self._fetch_oi(resolved, since)
        except NoData:
            raise
        except Exception as exc:
            self._last_error = str(exc)
            raise SourceError(str(exc)) from exc
        raise SourceError(f"{self.id} does not support {stream.value} fetch yet")

    async def stream(self, ref: object, stream: Stream) -> AsyncIterator[pd.DataFrame]:
        raise SourceError(f"{self.id} streaming is not implemented in this adapter slice")
        yield  # pragma: no cover

    def health(self) -> SourceHealth:
        return SourceHealth(source=self.id, status="degraded" if self._last_error else "closed", message=self._last_error)

    def _exchange(self) -> object:
        if self.exchange is not None:
            return self.exchange
        import ccxt  # type: ignore

        exchange_cls = getattr(ccxt, self.id)
        self.exchange = exchange_cls({"enableRateLimit": True, "timeout": 30000})
        return self.exchange

    def _fetch_candles(self, ref: SymbolRef, since: object | None) -> pd.DataFrame:
        rows = self._exchange().fetch_ohlcv(
            ref.to_ccxt(),
            timeframe=ref.timeframe or "1h",
            since=_to_ms_or_none(since),
            limit=1000,
        )
        if not rows:
            raise NoData(f"no candles for {ref.key()}")
        frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
        return frame

    def _fetch_funding(self, ref: SymbolRef, since: object | None) -> pd.DataFrame:
        rows = self._exchange().fetch_funding_rate_history(
            _futures_symbol(ref),
            since=_to_ms_or_none(since),
            limit=1000,
        )
        if not rows:
            raise NoData(f"no funding for {ref.key()}")
        return pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp(row["timestamp"], unit="ms", tz="UTC"),
                    "funding_rate": float(row["fundingRate"]),
                }
                for row in rows
                if row.get("fundingRate") is not None
            ]
        )

    def _fetch_oi(self, ref: SymbolRef, since: object | None) -> pd.DataFrame:
        rows = self._exchange().fetch_open_interest_history(
            _futures_symbol(ref),
            timeframe=ref.timeframe or "1h",
            since=_to_ms_or_none(since),
            limit=500,
        )
        if not rows:
            raise NoData(f"no open interest for {ref.key()}")
        return pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp(row["timestamp"], unit="ms", tz="UTC"),
                    "open_interest": float(row.get("openInterestAmount") or row.get("openInterest") or 0),
                }
                for row in rows
            ]
        )


def _futures_symbol(ref: SymbolRef) -> str:
    ccxt_symbol = ref.to_ccxt()
    if ":" in ccxt_symbol:
        return ccxt_symbol
    parts = ccxt_symbol.split("/")
    if len(parts) == 2:
        return f"{parts[0]}/{parts[1]}:{parts[1]}"
    return ccxt_symbol


def _to_ms_or_none(value: object | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return int(pd.Timestamp(value).timestamp() * 1000)
