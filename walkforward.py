
"""Walk-forward evaluation scaffold (Step 2).

Step 2 scope:
- Define windowing utilities and the orchestration boundaries.
- Step 3+ will implement parameter selection and OOS evaluation.

This file is intentionally a scaffold; the goal of Step 2 is to establish
stable module boundaries without prematurely implementing the full research system.
"""

import pandas as pd


def rolling_windows(start, end, in_sample_days, out_sample_days, tz="America/New_York"):
    """Yield (is_start, is_end, oos_start, oos_end) windows."""
    start = pd.Timestamp(start, tz=tz)
    end = pd.Timestamp(end, tz=tz)
    is_len = pd.Timedelta("%dd" % int(in_sample_days))
    oos_len = pd.Timedelta("%dd" % int(out_sample_days))

    t = start
    while t + is_len + oos_len <= end:
        is_start = t
        is_end = t + is_len
        oos_start = is_end
        oos_end = oos_start + oos_len
        yield (is_start, is_end, oos_start, oos_end)
        t = oos_start


def run_walkforward(*args, **kwargs):
    raise NotImplementedError("Step 3+ will implement walk-forward evaluation.")
