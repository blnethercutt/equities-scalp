
# Step 2 changelog — minimal refactor + research module scaffolding

This changelog documents **exactly** what changed in Step 2, relative to the provided baseline ZIP.

## Refactors (existing files)

### `main.py`
- **Moved** `ScalpAlgo` out of `main.py` into `algo.py`.
- `main.py` remains the live runner entrypoint and now imports `ScalpAlgo` from `algo.py`.
- No functional strategy logic changes were intentionally introduced.

### `risk.py`
- No changes (Step 2 requirement).  
  Replay compatibility is achieved by ensuring the simulated API supports:
  - `get_last_quote()` (bid/ask)
  - `get_account().equity`
  - `list_orders(status="open")` and optionally `cancel_all_orders()`

## New files (added)

### `algo.py`
- Contains the extracted `ScalpAlgo` class (minimal refactor only).

### `replay/` package
Added scaffolding modules to establish stable structure for Step 3:

- `replay/data_source.py`
  - Loads and normalizes historical bars from local CSV/Parquet.
  - Provides `BarsResult` with `.df` to mimic Alpaca bar results.

- `replay/broker.py`
  - In-memory broker state: cash, positions, orders.
  - Implements submit/cancel/query and mark-to-market account equity.

- `replay/fills.py`
  - Helper functions for synthetic quotes (spread), fee estimation, and partial fill capacity.

- `replay/sim_api.py`
  - `SimulatedAPI` facade implementing `BrokerDataAPI`.
  - Provides `get_bars().df`, `get_last_quote`, `get_last_trade`, `get_account().equity`,
    plus order/position methods used by the existing algo/risk code.

- `replay/runner.py`
  - Replay runner **skeleton only** (NotImplemented). Documents the intended sequencing.

- `replay/metrics.py`
  - Pure metric computation functions (expectancy, hit rate, avg win/loss, tail risk, time-in-trade).

- `replay/report.py`
  - JSON/CSV writers for metrics and equity curves.

- `replay/__init__.py`
  - Package initializer exporting the core contract types.

### Root-level scaffolding
- `research.py`
  - CLI scaffold for future replay/walk-forward runs (execution NotImplemented in Step 2).
- `walkforward.py`
  - Windowing utilities + NotImplemented walk-forward runner.

## Contract alignment updates (critical compatibility)

### `replay/contracts.py`
- `BrokerDataAPI.list_orders` now accepts `**kwargs` (required because `RiskManager` calls `list_orders(status="open")`).
- Added optional `cancel_all_orders()` method (used by `RiskManager` when present).
- `Order.submitted_at` type loosened to allow pandas Timestamp semantics used by `ScalpAlgo.checkup(...)`.

## Docs
- Updated `docs/REPLAY_DISCUSSION.md` to align with the current contract (`BrokerDataAPI`) and Step 2 scaffolding.

## Step 2 boundary
- No historical replay loop, fill simulation, or walk-forward optimization is implemented here.
  Those are Step 3+ tasks and are intentionally left as `NotImplementedError` stubs.
