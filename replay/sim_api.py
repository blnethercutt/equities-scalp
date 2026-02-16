
"""Simulated API facade (Step 2).

Step 2 scope:
- Provide an Alpaca-shaped API facade that conforms to `BrokerDataAPI`.
- Adapt local historical bars into an object with `.df` like Alpaca's bar result.
- Provide `get_last_quote()` and `get_account().equity` so `RiskManager` can operate.

This is NOT a full replay engine. The runner in Step 3 will:
- advance time,
- update market state (last trade/quote),
- decide fills, and
- call broker.apply_fill + emit order updates.
"""

import datetime as _dt
from typing import Any, Dict, Optional, Sequence

import pandas as pd

from .contracts import Bar, BrokerDataAPI, Quote, Trade, Account, Order, Position, ReplayFriction
from .data_source import BarsResult, HistoricalBarDataSource
from .fills import synthetic_quote_from_bar
from .broker import SimBroker


class SimulatedAPI(BrokerDataAPI):
    """A simulated broker+data API implementing `BrokerDataAPI`."""

    def __init__(self, data_source, broker=None, friction=None, tz="America/New_York"):
        if data_source is None:
            raise ValueError("data_source is required")
        self._ds = data_source
        self._broker = broker or SimBroker()
        self._friction = friction or ReplayFriction(
            spread_bps=0.0,
            spread_cents_min=0.0,
            commission_per_share=0.0,
            notional_fee_rate=0.0,
            participation_rate=1.0,
            activation_latency_bars=0,
        )
        self._tz = tz

        self._now = _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc)
        self._last_bar = {}  # symbol -> Bar
        self._mid = {}  # symbol -> mid price

    # ---- clock helpers (runner will set these) ----

    def _set_now(self, ts):
        # accepts datetime or pandas Timestamp
        if isinstance(ts, pd.Timestamp):
            if ts.tzinfo is None:
                ts = ts.tz_localize(self._tz)
            ts = ts.to_pydatetime()
        self._now = ts

    def update_market_from_bar(self, bar):
        """Update last trade/quote/mid based on a canonical Bar."""
        self._last_bar[bar.symbol] = bar
        self._mid[bar.symbol] = float(bar.close)

    # ---- Market data ----

    def get_bars(self, symbol, timeframe, start, end, **kwargs):
        # `ScalpAlgo` passes dates as YYYY-MM-DD strings and expects `.df`.
        df = self._ds.get_bars(symbol, start=start, end=end)
        return BarsResult(df)

    def get_last_trade(self, symbol):
        bar = self._last_bar.get(symbol)
        if bar is None:
            # fallback: attempt to load the last row
            df = self._ds.get_bars(symbol)
            if len(df) == 0:
                raise ValueError("no data for symbol: %s" % symbol)
            row = df.iloc[-1]
            ts = df.index[-1].to_pydatetime()
            bar = Bar(
                symbol=symbol,
                timestamp=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            )
            self.update_market_from_bar(bar)
        return Trade(symbol=symbol, timestamp=bar.timestamp, price=float(bar.close), size=float(bar.volume))

    def get_last_quote(self, symbol):
        bar = self._last_bar.get(symbol)
        if bar is None:
            # synthesize from last trade
            t = self.get_last_trade(symbol)
            bar = Bar(symbol=symbol, timestamp=t.timestamp, open=t.price, high=t.price, low=t.price, close=t.price, volume=t.size)
        return synthetic_quote_from_bar(bar, self._friction)

    # ---- Orders ----

    def submit_order(self, symbol, side, type, qty, time_in_force, limit_price=None, **kwargs):
        submitted_at = kwargs.get("submitted_at") or self._now
        return self._broker.submit_order(
            symbol=symbol,
            side=side,
            type=type,
            qty=qty,
            time_in_force=time_in_force,
            limit_price=limit_price,
            submitted_at=submitted_at,
            **kwargs
        )

    def cancel_order(self, order_id):
        return self._broker.cancel_order(order_id)

    def cancel_all_orders(self, **kwargs):
        return self._broker.cancel_all_orders()

    def get_order(self, order_id):
        return self._broker.get_order(order_id)

    def list_orders(self, **kwargs):
        return self._broker.list_orders(**kwargs)

    # ---- Portfolio / account ----

    def get_position(self, symbol):
        return self._broker.get_position(symbol)

    def list_positions(self):
        return self._broker.list_positions()

    def get_account(self):
        return self._broker.account(mid_prices=self._mid)
