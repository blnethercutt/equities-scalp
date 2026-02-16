# Research + Replay Contract (Step 1 Only)

This document defines a **stable, explicit contract** for adding a historical **research + replay loop** to this repository.

**Scope of this step:**
- Define the interface boundary that allows the existing `ScalpAlgo` + `RiskManager` to run unchanged against either:
  - the live Alpaca API, or
  - a simulated API backed by historical data.

**Out of scope (not implemented in Step 1):**
- A replay engine, fill simulator, walk-forward loop, parameter search, metrics pipeline.

---

## Design goals

1. **No strategy rewrite.** The live strategy code should not need to know whether it is trading live or replay.
2. **Deterministic replays.** Given the same historical data + parameters, results should be identical.
3. **Realistic friction.** Replay must be able to model at least:
   - spread,
   - fees,
   - partial fills,
   - (optional) order activation latency.
4. **Walk-forward friendly.** All replay parameters and strategy knobs must be externally configurable.

---

## Objects and terminology

### Time
- All timestamps MUST be timezone-aware `datetime` values.
- Replay will operate on a fixed bar resolution (initially `1Min`).

### Market data primitives

**Bar** (1-minute OHLCV)
- `symbol: str`
- `timestamp: datetime` (bar timestamp; see `bar_timestamp_semantics` below)
- `open: float`
- `high: float`
- `low: float`
- `close: float`
- `volume: float`

**Quote** (synthetic or historical)
- `symbol: str`
- `timestamp: datetime`
- `bid_price: float`
- `ask_price: float`
- `bid_size: float | None`
- `ask_size: float | None`

**Trade** (last trade)
- `symbol: str`
- `timestamp: datetime`
- `price: float`
- `size: float | None`

### Trading primitives

**Order**
- `id: str`
- `symbol: str`
- `side: 'buy' | 'sell'`
- `type: 'market' | 'limit'`
- `time_in_force: 'day' | 'gtc'` (initially support `day`)
- `qty: float`
- `limit_price: float | None`
- `status: 'new' | 'partially_filled' | 'filled' | 'canceled' | 'rejected'`
- `filled_qty: float`
- `filled_avg_price: float | None`
- `submitted_at: datetime`

**Position**
- `symbol: str`
- `qty: float`
- `avg_entry_price: float`

**Account**
- `equity: float` (cash + marked-to-market value)
- `cash: float`
- `buying_power: float | None`

---

## Contract: Alpaca-shaped API surface

The simulated API MUST implement the subset of Alpaca REST + streaming used by this repository.

### Required methods (used by existing code)

These methods are currently called by `main.py` and/or `risk.py`.

1. **Bars bootstrap**
   - `get_bars(symbol: str, timeframe, start: str, end: str, adjustment: str = 'raw') -> object`
     - The returned object MUST have a `.df` attribute compatible with current usage:
       - `api.get_bars(...).df` returns a pandas DataFrame containing at least:
         - `open, high, low, close, volume`
       - indexed by timestamp.

2. **Market marks**
   - `get_last_trade(symbol: str) -> TradeLike`
     - Returned object MUST have `.price` (float) and ideally `.timestamp`.
   - `get_last_quote(symbol: str) -> QuoteLike`
     - Returned object MUST have `.bidprice` and `.askprice` attributes (note Alpaca naming), and ideally `.timestamp`.

3. **Orders**
   - `submit_order(**kwargs) -> OrderLike`
     - Must accept parameters used in this codebase:
       - `symbol, side, type, qty, time_in_force, limit_price (optional)`
     - Return MUST include `.id`, `.symbol`, `.side`, `.qty`, `.limit_price`, `.submitted_at`.
   - `cancel_order(order_id: str) -> None`
   - `get_order(order_id: str) -> OrderLike`
   - `list_orders(**kwargs) -> List[OrderLike]`
     - Existing code calls `list_orders()` without arguments.

4. **Positions/account**
   - `list_positions() -> List[PositionLike]`
   - `get_position(symbol: str) -> PositionLike`
   - `get_account() -> AccountLike`
     - Existing risk controls read `.equity`.

5. **Clock (live only)**
   - `get_clock() -> ClockLike`
     - Only used in the live periodic loop.
     - Replay can omit this if replay runner does not call it.

### Attribute compatibility

To reduce code changes, replay objects should expose Alpaca-like attribute names where the repo expects them.

- Quotes: `.bidprice` and `.askprice` (not `bid_price/ask_price`).
- Orders: `.submitted_at` is timezone-aware.

---

## Replay realism knobs (minimum viable set)

Replay must be parameterized, not hardcoded.

### Spread
Two supported modes:
1. **Synthetic spread from mid** (default when quote history is absent)
   - `mid = close`
   - `spread = max(spread_cents_min, spread_bps * mid / 10_000)`
   - `bid = mid - spread/2`
   - `ask = mid + spread/2`

2. **Historical quotes** (optional)
   - Use best bid/ask at the event timestamp.

### Fees
- `commission_per_share` (float)
- `fee_rate_bps` applied to notional (optional)

### Partial fills
A simple, controllable model:
- `participation_rate` in `(0, 1]`
- per bar, max fillable shares = `bar.volume * participation_rate`

### Order activation latency (optional but recommended)
- `latency_bars` (int >= 0)
- orders submitted during bar `t` become eligible for fills at bar `t + latency_bars`.

---

## Bar timestamp semantics (must be explicit)

To prevent subtle lookahead:

- The replay runner MUST define whether a bar timestamp represents:
  - bar **open** time, or
  - bar **close** time.

**Recommended for this codebase:** treat `bar.timestamp` as the **bar close** time and enforce `latency_bars >= 1` by default so orders placed on a bar cannot fill on that same close.

---

## What Step 2 will implement (for later)

Once this contract is accepted, the next step is to implement:
- `replay/sim_api.py`: a concrete simulated API implementing this contract
- `replay/runner.py`: the loop that feeds bars + order updates into `ScalpAlgo`
- `replay/metrics.py`: extraction of expectancy/hit rate/avg win-loss/tail risk/time-in-trade
- `walkforward.py`: rolling IS/OOS evaluation and parameter selection

