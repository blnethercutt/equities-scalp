
"""Fill and friction helpers (Step 2).

Step 2 scope:
- Define primitives that model spread + fees + partial fill capacity.
- Provide deterministic helpers the future replay runner can use.

Step 3 will implement the full replay loop and apply these primitives
to generate order-update events and fills.
"""

import datetime as _dt
from typing import Optional, Tuple

from .contracts import Bar, Quote, ReplayFriction


def synthetic_quote_from_bar(bar: Bar, friction: ReplayFriction) -> Quote:
    """Create a synthetic quote from an OHLCV bar using a mid=close model.

    Spread model:
    - spread = max(spread_cents_min, spread_bps * mid / 10000)
    - bid = mid - spread/2
    - ask = mid + spread/2
    """
    mid = float(bar.close)
    spread = max(float(friction.spread_cents_min), float(friction.spread_bps) * mid / 10000.0)
    bid = mid - spread / 2.0
    ask = mid + spread / 2.0

    return Quote(
        symbol=bar.symbol,
        timestamp=bar.timestamp,
        bid_price=bid,
        ask_price=ask,
        bid_size=float(bar.volume),
        ask_size=float(bar.volume),
    )


def estimate_fee(notional: float, shares: float, friction: ReplayFriction) -> float:
    """Estimate fees for a fill (commission + notional-based fee proxy)."""
    commission = float(friction.commission_per_share) * float(shares)
    notional_fee = float(friction.notional_fee_rate) * float(notional)
    return commission + notional_fee


def max_fillable_shares(bar: Bar, friction: ReplayFriction) -> float:
    """Upper bound on shares fillable this bar, for partial-fill modeling."""
    return float(bar.volume) * float(friction.participation_rate)


def limit_buy_marketable(bar: Bar, friction: ReplayFriction, limit_price: float) -> bool:
    """Conservative check: buy limit is marketable if synthetic ask_low <= limit."""
    q = synthetic_quote_from_bar(bar, friction)
    ask_low = float(bar.low) + (q.ask_price - q.bid_price) / 2.0
    return ask_low <= float(limit_price)


def limit_sell_marketable(bar: Bar, friction: ReplayFriction, limit_price: float) -> bool:
    """Conservative check: sell limit is marketable if synthetic bid_high >= limit."""
    q = synthetic_quote_from_bar(bar, friction)
    bid_high = float(bar.high) - (q.ask_price - q.bid_price) / 2.0
    return bid_high >= float(limit_price)
