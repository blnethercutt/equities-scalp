
"""In-memory broker/account state (Step 2).

Step 2 scope:
- Provide a simple order/position/account container suitable for replay.
- Implement order submission/cancel/query and mark-to-market equity calculation.

Step 3 will:
- decide fills using the fill model, and
- call `apply_fill(...)` to update broker state and emit order updates.

This module is intentionally deterministic and side-effect free (no I/O).
"""

import datetime as _dt
from typing import Any, Dict, List, Optional, Sequence

from .contracts import Account, Order, OrderEvent, OrderStatus, Position


class SimBroker(object):
    """A minimal simulated broker.

    This broker tracks:
    - cash
    - positions
    - orders and open order set

    It does NOT implement market data or fill decisions.
    """

    def __init__(self, starting_cash=100000.0):
        self._cash = float(starting_cash)
        self._positions = {}  # symbol -> Position
        self._orders = {}  # order_id -> Order
        self._open_ids = set()
        self._order_seq = 0

    # ---- basic accessors ----

    def cash(self):
        return self._cash

    def list_positions(self):
        return list(self._positions.values())

    def get_position(self, symbol):
        return self._positions.get(symbol, Position(symbol=symbol, qty=0.0, avg_entry_price=0.0))

    def list_orders(self, **kwargs):
        # Support Alpaca-style filter: status="open"
        status = kwargs.get("status")
        if status is None:
            return [self._orders[oid] for oid in sorted(self._orders.keys())]
        status = str(status).lower()
        if status == "open":
            return [self._orders[oid] for oid in sorted(self._open_ids)]
        if status == "closed":
            return [o for o in self._orders.values() if o.id not in self._open_ids]
        return [o for o in self._orders.values() if str(o.status).lower() == status]

    def get_order(self, order_id):
        return self._orders[order_id]

    # ---- order lifecycle ----

    def submit_order(
        self,
        symbol,
        side,
        type,
        qty,
        time_in_force,
        limit_price=None,
        submitted_at=None,
        **kwargs
    ):
        self._order_seq += 1
        oid = "SIM-%08d" % self._order_seq
        if submitted_at is None:
            submitted_at = _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc)

        status = OrderStatus.NEW.value
        o = Order(
            id=oid,
            symbol=symbol,
            side=side,
            type=type,
            time_in_force=time_in_force,
            qty=float(qty),
            limit_price=float(limit_price) if limit_price is not None else None,
            status=status,
            submitted_at=submitted_at,
            filled_qty=0.0,
            filled_avg_price=None,
        )
        self._orders[oid] = o
        self._open_ids.add(oid)
        return o

    def cancel_order(self, order_id):
        if order_id not in self._orders:
            return
        o = self._orders[order_id]
        o.status = OrderStatus.CANCELED.value
        if order_id in self._open_ids:
            self._open_ids.remove(order_id)

    def cancel_all_orders(self):
        for oid in list(self._open_ids):
            self.cancel_order(oid)

    # ---- fills + accounting ----

    def apply_fill(self, order_id, fill_qty, fill_price, fee=0.0):
        """Apply a fill to broker state.

        Step 2 note: This is provided to support Step 3 without changing
        the broker API surface later.
        """
        o = self._orders[order_id]
        fq = float(fill_qty)
        fp = float(fill_price)
        fee = float(fee)

        new_filled = float(o.filled_qty) + fq
        o.filled_qty = new_filled

        if o.filled_avg_price is None:
            o.filled_avg_price = fp
        else:
            # volume-weighted average
            prev_notional = float(o.filled_avg_price) * (new_filled - fq)
            o.filled_avg_price = (prev_notional + fp * fq) / new_filled

        # Determine if order is complete
        if new_filled + 1e-9 >= float(o.qty):
            o.status = OrderStatus.FILLED.value
            if order_id in self._open_ids:
                self._open_ids.remove(order_id)
        else:
            o.status = OrderStatus.PARTIALLY_FILLED.value

        # Update cash/positions
        notional = fq * fp
        if str(o.side).lower() == "buy":
            self._cash -= notional
            self._cash -= fee
            pos = self.get_position(o.symbol)
            new_qty = float(pos.qty) + fq
            if new_qty <= 0:
                self._positions.pop(o.symbol, None)
            else:
                # update avg entry
                prev_notional = float(pos.avg_entry_price) * float(pos.qty)
                new_avg = (prev_notional + notional) / new_qty if new_qty else 0.0
                self._positions[o.symbol] = Position(symbol=o.symbol, qty=new_qty, avg_entry_price=new_avg)
        else:
            self._cash += notional
            self._cash -= fee
            pos = self.get_position(o.symbol)
            new_qty = float(pos.qty) - fq
            if new_qty <= 0:
                self._positions.pop(o.symbol, None)
            else:
                self._positions[o.symbol] = Position(symbol=o.symbol, qty=new_qty, avg_entry_price=float(pos.avg_entry_price))

        return o

    def account(self, mid_prices):
        """Compute account snapshot given mid prices dict."""
        equity = float(self._cash)
        for sym, pos in self._positions.items():
            mid = float(mid_prices.get(sym, float(pos.avg_entry_price) or 0.0))
            equity += float(pos.qty) * mid
        buying_power = equity  # simple proxy; Step 3 can refine
        return Account(equity=equity, cash=float(self._cash), buying_power=buying_power)
