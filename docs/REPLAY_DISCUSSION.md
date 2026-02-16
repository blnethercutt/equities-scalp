# Chat discussion protocol for Research + Replay (Step 1)

Use this file as the **shared checklist** when we discuss the replay system in chat.

## Round 1 — Contract freeze

Goal: finalize the boundary so we can build replay without refactoring the strategy again.

1) Confirm API surface (see `replay/contracts.py::BrokerAPI`):
   - Market data: `get_bars`, `get_last_quote`, `get_last_trade`
   - Orders: `submit_order`, `cancel_order`, `get_order`, `list_orders`
   - Portfolio: `get_account`, `list_positions`, `get_position`
   - Clock: `get_clock`

2) Confirm object shapes (see `replay/contracts.py`):
   - `Bar`, `Quote`, `Trade`
   - `Order`, `Position`, `Account`, `Clock`

3) Confirm replay parameters (see `ReplayParams`):
   - spread: `spread_bps`, `spread_cents_min`
   - fees: `commission_per_share`, `reg_fee_rate`
   - partial fills: `participation_rate`
   - activation latency: `latency_bars`
   - fill price convention: `fill_price_policy`

Deliverable: any contract edits are done **here** before writing replay logic.

## Round 2 — Fill semantics validation

Goal: eliminate optimistic backtest behavior.

Walk through 3 adversarial examples with explicit numbers:
1) Rising bar with a buy limit near the ask
2) Falling bar with a sell limit near the bid
3) Low-volume bar where partial fills occur across multiple minutes

Decisions to lock:
- when orders become active (same bar vs next bar)
- how OHLC is used to decide fillable vs not-fillable
- how partial fills clip to `participation_rate` of bar volume

## Round 3 — Walk-forward protocol

Goal: define the exact evaluation methodology.

1) Windowing:
   - in-sample length (days)
   - out-of-sample length (days)
   - step size

2) Parameter search:
   - which parameters can vary (keep minimal to avoid p-hacking)
   - grid ranges

3) Selection objective and constraints:
   - primary objective: expectancy and/or net PnL
   - constraints: max drawdown, worst trade, turnover/time-in-market

4) Output artifacts:
   - per-window metrics JSON/CSV
   - aggregate OOS metrics
   - equity curve series

