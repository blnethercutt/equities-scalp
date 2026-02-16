
"""Reporting helpers (Step 2).

Step 2 scope:
- Define basic output writers for metrics and time series.
- Keep outputs deterministic and human-reviewable (JSON/CSV).

Step 3 will provide actual replay results (fills/trades/equity curve) to write.
"""

import json
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd


def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)


def write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)


def write_trades_csv(path, trades):
    """Write trades to CSV.

    `trades` can be a list of objects with __dict__-like fields, or dicts.
    """
    rows = []
    for t in trades:
        if isinstance(t, dict):
            rows.append(t)
        else:
            rows.append({k: getattr(t, k) for k in getattr(t, "__slots__", [])})
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)


def write_equity_curve_csv(path, equity_points):
    """Write equity curve points to CSV.

    equity_points: iterable of dicts: {timestamp, equity, cash, ...}
    """
    df = pd.DataFrame(list(equity_points))
    df.to_csv(path, index=False)
