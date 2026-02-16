
"""Historical data loading utilities (Step 2).

Step 2 scope:
- Provide a small, reusable data source that can load minute bars from local files.
- Normalize schema so the future replay engine can align multiple symbols on a shared timeline.

This module deliberately does NOT implement a full replay loop. It only provides
I/O and normalization primitives that later modules can consume.

Expected canonical bar schema:
- index: timezone-aware pandas DatetimeIndex
- columns: ["symbol","open","high","low","close","volume"]

Notes
-----
`ScalpAlgo` currently expects Alpaca-style bars results where `result.df`
includes a `symbol` column and OHLCV columns. The simulated API wrapper
will adapt to that shape.
"""

import os
import pandas as pd


class BarsResult(object):
    """Simple container mimicking `alpaca_trade_api` bar result objects.

    Alpaca's `get_bars(...)` returns an object with a `.df` attribute.
    We mirror that to avoid invasive changes to existing strategy code.
    """

    __slots__ = ("df",)

    def __init__(self, df):
        self.df = df


def _ensure_tz(df, tz):
    if df.index.tz is None:
        return df.tz_localize(tz)
    return df.tz_convert(tz)


def normalize_bars(df, symbol, tz="America/New_York"):
    """Normalize a DataFrame to the canonical schema.

    Accepts common column variations (case-insensitive).
    """
    if df is None or len(df) == 0:
        raise ValueError("empty bars dataframe")

    # Standardize column names
    cols = {c.lower(): c for c in df.columns}
    def pick(name):
        if name in df.columns:
            return name
        if name.lower() in cols:
            return cols[name.lower()]
        return None

    open_c = pick("open")
    high_c = pick("high")
    low_c = pick("low")
    close_c = pick("close")
    vol_c = pick("volume")

    if close_c is None:
        raise ValueError("bars must include a close column")

    # Ensure datetime index
    if not isinstance(df.index, pd.DatetimeIndex):
        # try common timestamp columns
        tcol = pick("timestamp") or pick("time") or pick("date")
        if tcol is None:
            raise ValueError("bars must have a DatetimeIndex or a timestamp column")
        df = df.set_index(pd.to_datetime(df[tcol]))
    df = df.sort_index()
    df.index = pd.DatetimeIndex(df.index)
    df = _ensure_tz(df, tz)

    out = pd.DataFrame(index=df.index)
    out["symbol"] = symbol
    if open_c is not None:
        out["open"] = df[open_c].astype(float)
    else:
        out["open"] = df[close_c].astype(float)

    out["high"] = df[high_c].astype(float) if high_c is not None else out["open"]
    out["low"] = df[low_c].astype(float) if low_c is not None else out["open"]
    out["close"] = df[close_c].astype(float)
    out["volume"] = df[vol_c].astype(float) if vol_c is not None else 0.0

    return out


class HistoricalBarDataSource(object):
    """Load bars from a local file per symbol.

    Supported formats:
    - CSV
    - Parquet (requires pyarrow/fastparquet installed)

    The user supplies a mapping symbol -> filepath.
    """

    def __init__(self, symbol_to_path, tz="America/New_York", cache_dir=None):
        self._paths = dict(symbol_to_path or {})
        self._tz = tz
        self._cache_dir = cache_dir

    @property
    def tz(self):
        return self._tz

    def load(self, symbol):
        if symbol not in self._paths:
            raise KeyError("no path configured for symbol: %s" % symbol)
        path = self._paths[symbol]
        if not os.path.exists(path):
            raise IOError("bars file not found: %s" % path)

        if path.lower().endswith(".csv"):
            df = pd.read_csv(path)
        elif path.lower().endswith(".parquet"):
            df = pd.read_parquet(path)
        else:
            raise ValueError("unsupported bars file type: %s" % path)

        return normalize_bars(df, symbol=symbol, tz=self._tz)

    def get_bars(self, symbol, start=None, end=None):
        """Return canonical bars for [start, end)."""
        df = self.load(symbol)
        if start is not None:
            start_ts = pd.to_datetime(start)
            if start_ts.tzinfo is None:
                start_ts = start_ts.tz_localize(self._tz)
            df = df[df.index >= start_ts]
        if end is not None:
            end_ts = pd.to_datetime(end)
            if end_ts.tzinfo is None:
                end_ts = end_ts.tz_localize(self._tz)
            df = df[df.index < end_ts]
        return df

    def bars_result(self, symbol, start=None, end=None):
        """Return a BarsResult (`.df`) for compatibility with `ScalpAlgo`."""
        df = self.get_bars(symbol, start=start, end=end)
        return BarsResult(df)
