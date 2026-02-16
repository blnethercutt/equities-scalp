
"""Metrics primitives for replay evaluation (Step 2).

Step 2 scope:
- Define the core metric computations requested:
  expectancy, hit rate, avg win/loss, tail risk, time-in-trade.
- Provide these as pure functions over trade records.

Step 3 will implement trade extraction (fills -> round trips).
"""

import math
import datetime as _dt
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


class TradeRecord(object):
    """Minimal trade record for metrics.

    Fields are intentionally simple and serializable.
    """

    __slots__ = ("symbol", "entry_ts", "exit_ts", "qty", "entry_price", "exit_price", "fees", "pnl_net")

    def __init__(self, symbol, entry_ts, exit_ts, qty, entry_price, exit_price, fees, pnl_net):
        self.symbol = symbol
        self.entry_ts = entry_ts
        self.exit_ts = exit_ts
        self.qty = float(qty)
        self.entry_price = float(entry_price)
        self.exit_price = float(exit_price)
        self.fees = float(fees)
        self.pnl_net = float(pnl_net)

    def time_in_trade_seconds(self):
        if self.entry_ts is None or self.exit_ts is None:
            return None
        dt = self.exit_ts - self.entry_ts
        return dt.total_seconds() if hasattr(dt, "total_seconds") else None


def hit_rate(trades):
    trades = list(trades)
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.pnl_net > 0)
    return wins / float(len(trades))


def avg_win_loss(trades):
    wins = [t.pnl_net for t in trades if t.pnl_net > 0]
    losses = [t.pnl_net for t in trades if t.pnl_net < 0]
    avg_win = sum(wins) / float(len(wins)) if wins else 0.0
    avg_loss = sum(losses) / float(len(losses)) if losses else 0.0
    return avg_win, avg_loss


def expectancy(trades):
    trades = list(trades)
    if not trades:
        return 0.0
    hr = hit_rate(trades)
    aw, al = avg_win_loss(trades)
    # Note: al is negative
    return hr * aw + (1.0 - hr) * al


def equity_curve_drawdown(equity_series):
    """Compute max drawdown from an equity series (list of floats)."""
    if not equity_series:
        return 0.0
    peak = equity_series[0]
    max_dd = 0.0
    for x in equity_series:
        peak = max(peak, x)
        dd = (peak - x)
        max_dd = max(max_dd, dd)
    return max_dd


def tail_risk(trades, equity_series=None):
    """Return a minimal tail risk summary.

    Includes:
    - worst_trade
    - max_drawdown (if equity series supplied)
    """
    trades = list(trades)
    worst_trade = min((t.pnl_net for t in trades), default=0.0)
    out = {"worst_trade": worst_trade}
    if equity_series is not None:
        out["max_drawdown"] = equity_curve_drawdown(list(equity_series))
    return out


def time_in_trade_stats(trades):
    """Return mean/median/p95 time-in-trade in seconds."""
    xs = [t.time_in_trade_seconds() for t in trades]
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"mean": 0.0, "median": 0.0, "p95": 0.0}
    xs_sorted = sorted(xs)
    mean = sum(xs_sorted) / float(len(xs_sorted))
    median = xs_sorted[len(xs_sorted)//2]
    p95_idx = int(math.ceil(0.95 * len(xs_sorted))) - 1
    p95_idx = max(0, min(p95_idx, len(xs_sorted)-1))
    p95 = xs_sorted[p95_idx]
    return {"mean": mean, "median": median, "p95": p95}


def summarize(trades, equity_series=None):
    trades = list(trades)
    aw, al = avg_win_loss(trades)
    return {
        "count": len(trades),
        "hit_rate": hit_rate(trades),
        "avg_win": aw,
        "avg_loss": al,
        "expectancy": expectancy(trades),
        "tail_risk": tail_risk(trades, equity_series=equity_series),
        "time_in_trade": time_in_trade_stats(trades),
    }
