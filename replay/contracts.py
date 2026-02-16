"""Contracts for research/replay integration (Step 1).

This module defines **interfaces and value objects** that a simulated broker + data
adapter must implement so that the existing algo (`ScalpAlgo`) and risk overlay
(`RiskManager`) can run against historical data without invasive rewrites.

IMPORTANT
---------
This file intentionally contains *no implementation* of replay, fills, or metrics.
It is a stable contract to be implemented in later steps.

Python compatibility: keeps to stdlib for the current repo's Python 3.6 target.
"""

import abc
import datetime as _dt
from enum import Enum
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence


# ----------------------------
# Market data value objects
# ----------------------------


class Bar(NamedTuple):
    """A time-bucketed bar.

    Notes:
    - `timestamp` MUST be timezone-aware (UTC recommended).
    - Prices are floats (USD).
    - `volume` is shares.

    This mirrors the bar object shape used in `main.py` (`bar.open`, `bar.close`, etc.).
    """

    symbol: str
    timestamp: _dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class Quote(NamedTuple):
    """Top-of-book quote.

    Notes:
    - `timestamp` MUST be timezone-aware (UTC recommended).
    - `bid_size` / `ask_size` are shares.

    `RiskManager` consumes `get_last_quote()` and expects `bid_price` and `ask_price`.
    """

    symbol: str
    timestamp: _dt.datetime
    bid_price: float
    ask_price: float
    bid_size: float
    ask_size: float


class Trade(NamedTuple):
    """Last trade print."""

    symbol: str
    timestamp: _dt.datetime
    price: float
    size: float


# ----------------------------
# Brokerage / OMS value objects
# ----------------------------


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"


class OrderStatus(str, Enum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


class OrderEvent(str, Enum):
    """Order update events emitted to algo handlers.

    This mirrors Alpaca stream event names used in `main.py`:
    - 'fill'
    - 'partial_fill'
    - 'canceled'
    - 'rejected'

    The replay engine must emit these *exact strings* to keep algo logic unchanged.
    """

    FILL = "fill"
    PARTIAL_FILL = "partial_fill"
    CANCELED = "canceled"
    REJECTED = "rejected"


class Order(object):
    """A minimal order object compatible with the fields used in this repo.

    The existing algo expects attributes like:
    - id
    - symbol
    - side
    - qty
    - limit_price (optional)
    - submitted_at (timezone-aware datetime)

    and sometimes reads filled fields on the *order update payload*:
    - filled_qty
    - filled_avg_price

    In live trading, Alpaca provides rich order objects/dicts. In replay,
    we standardize a small subset.
    """

    __slots__ = (
        "id",
        "symbol",
        "side",
        "type",
        "time_in_force",
        "qty",
        "limit_price",
        "status",
        "submitted_at",
        "filled_qty",
        "filled_avg_price",
    )

    def __init__(
        self,
        id: str,
        symbol: str,
        side: str,
        type: str,
        time_in_force: str,
        qty: float,
        limit_price: Optional[float],
        status: str,
        submitted_at: _dt.datetime,
        filled_qty: float = 0.0,
        filled_avg_price: Optional[float] = None,
    ) -> None:
        self.id = id
        self.symbol = symbol
        self.side = side
        self.type = type
        self.time_in_force = time_in_force
        self.qty = qty
        self.limit_price = limit_price
        self.status = status
        self.submitted_at = submitted_at
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price

    def to_update_dict(self) -> Dict[str, Any]:
        """Return a dict-shaped representation similar to Alpaca trade updates.

        The current algo sometimes treats update payloads as dicts.
        """

        return {
            "id": self.id,
            "symbol": self.symbol,
            "side": self.side,
            "type": self.type,
            "time_in_force": self.time_in_force,
            "qty": self.qty,
            "limit_price": self.limit_price,
            "status": self.status,
            "submitted_at": self.submitted_at,
            "filled_qty": self.filled_qty,
            "filled_avg_price": self.filled_avg_price,
        }


class Position(object):
    """A minimal position object compatible with this repo."""

    __slots__ = ("symbol", "qty", "avg_entry_price")

    def __init__(self, symbol: str, qty: float, avg_entry_price: float) -> None:
        self.symbol = symbol
        self.qty = qty
        self.avg_entry_price = avg_entry_price


class Account(object):
    """A minimal account object.

    `RiskManager` uses `account.equity` in multiple places.
    """

    __slots__ = ("equity", "cash", "buying_power")

    def __init__(self, equity: float, cash: float, buying_power: float) -> None:
        self.equity = equity
        self.cash = cash
        self.buying_power = buying_power


# ----------------------------
# Replay friction configuration
# ----------------------------


class ReplayFriction(NamedTuple):
    """Parameters controlling realism/friction in replay.

    All values are *inputs* to the later replay engine.

    Required by spec:
    - spread
    - fees
    - partial fills

    Additional realism knob:
    - order activation latency (bars)
    """

    # Spread model
    spread_bps: float  # e.g., 10 = 10 bps
    spread_cents_min: float  # e.g., 1.0 = $0.01

    # Fees model
    commission_per_share: float  # e.g., 0.0 for commission-free
    notional_fee_rate: float  # e.g., 0.0 or small regulatory fee proxy

    # Partial fill model
    participation_rate: float  # fraction of bar volume available to fills

    # Latency model
    activation_latency_bars: int  # orders submitted on bar t become active at t+N


# ----------------------------
# Broker/Data API contract
# ----------------------------


class BrokerDataAPI(abc.ABC):
    """Contract that both live API wrappers and simulated APIs can implement.

    This is intentionally shaped to cover what `main.py` + `risk.py` call today.

    The replay engine will:
    - implement this API, and
    - drive the algo by calling `ScalpAlgo.on_bar(...)` and `ScalpAlgo.on_order_update(...)`.

    Live trading continues to use Alpaca's objects directly; replay uses this contract.
    """

    # ----- Market data -----

    @abc.abstractmethod
    def get_bars(
        self,
        symbol: str,
        timeframe: Any,
        start: str,
        end: str,
        adjustment: str = "raw",
    ) -> Any:
        """Return historical bars.

        Live Alpaca returns an object with `.df`.

        For replay, you may return a thin wrapper that has `.df` in the same shape,
        or (in later steps) adapt the algo to accept a DataFrame directly.
        """

    @abc.abstractmethod
    def get_last_trade(self, symbol: str) -> Trade:
        """Return most recent trade for symbol."""

    @abc.abstractmethod
    def get_last_quote(self, symbol: str) -> Quote:
        """Return most recent quote for symbol."""

    # ----- Orders / OMS -----

    @abc.abstractmethod
    def submit_order(
        self,
        symbol: str,
        side: str,
        type: str,
        qty: float,
        time_in_force: str,
        limit_price: Optional[float] = None,
        **kwargs: Any
    ) -> Order:
        """Submit a new order and return an order object."""

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> None:
        """Cancel an existing order."""

    @abc.abstractmethod
    def get_order(self, order_id: str) -> Order:
        """Fetch an order by id."""

    @abc.abstractmethod
    def list_orders(self) -> Sequence[Order]:
        """List current open orders (or all recent orders depending on implementation)."""

    # ----- Portfolio / account -----

    @abc.abstractmethod
    def get_position(self, symbol: str) -> Position:
        """Get current position for symbol."""

    @abc.abstractmethod
    def list_positions(self) -> Sequence[Position]:
        """List all current positions."""

    @abc.abstractmethod
    def get_account(self) -> Account:
        """Return account snapshot (must include equity)."""


# ----------------------------
# Notes for Step 2+ implementers
# ----------------------------


REPLAY_NOTES: str = """
When implementing the replay engine (later steps), ensure:

1) Event ordering is realistic:
   - Process fills/order updates for time t
   - Then deliver bar(t) to algo
   - Orders submitted by algo during bar(t) should not fill until t + activation_latency_bars

2) Use conservative fill assumptions when only OHLCV data is available.

3) Emit order updates using the exact string events expected by the algo:
   - 'fill', 'partial_fill', 'canceled', 'rejected'

4) Maintain timezone awareness. Use UTC internally and convert only at boundaries.
"""
