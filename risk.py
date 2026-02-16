"""
risk.py

Risk management overlay for the example scalping script.

Design goals:
- Portfolio-level exposure limits (max concurrent positions, max total exposure)
- Daily loss kill-switch (equity drawdown-based)
- Per-position risk controls (stop-loss %, time-stop minutes)
- Guardrails to avoid trading during poor microstructure (spread) and abnormal bars (volatility)
- Per-symbol circuit breaker (disable trading after repeated forced exits)

This module is intentionally dependency-light and compatible with Python 3.6+.
"""

import logging
import time
from collections import defaultdict

import pandas as pd


class RiskConfig(object):
    """
    Configuration container (no dataclasses for Python 3.6 compatibility).
    All monetary values are in USD.
    """

    def __init__(
        self,
        max_positions=3,
        max_position_notional=None,
        max_total_exposure=None,
        max_daily_loss=100.0,
        stop_loss_pct=0.003,        # 0.30%
        time_stop_minutes=10.0,
        max_spread_bps=25.0,        # 0.25%
        max_spread_cents=None,
        max_bar_range_pct=0.01,     # 1.0% high-low range / close
        max_return_std_pct=0.01,    # 1.0% std dev of 1-min returns (last 20)
        symbol_max_forced_exits=2,
        forced_exit_cooldown_minutes=0.0,
        enable_spread_guard=True,
        enable_volatility_guard=True,
    ):
        self.max_positions = int(max_positions)

        # If None, caller should set based on --lot or account size.
        self.max_position_notional = max_position_notional  # float or None
        self.max_total_exposure = max_total_exposure        # float or None

        self.max_daily_loss = float(max_daily_loss)

        self.stop_loss_pct = float(stop_loss_pct)
        self.time_stop_minutes = float(time_stop_minutes)

        self.max_spread_bps = float(max_spread_bps)
        self.max_spread_cents = max_spread_cents if max_spread_cents is None else float(max_spread_cents)

        self.max_bar_range_pct = float(max_bar_range_pct)
        self.max_return_std_pct = float(max_return_std_pct)

        self.symbol_max_forced_exits = int(symbol_max_forced_exits)
        self.forced_exit_cooldown_minutes = float(forced_exit_cooldown_minutes)

        self.enable_spread_guard = bool(enable_spread_guard)
        self.enable_volatility_guard = bool(enable_volatility_guard)


class RiskDecision(object):
    """Simple return object for risk checks."""
    def __init__(self, ok, reason="", qty=None):
        self.ok = bool(ok)
        self.reason = str(reason) if reason else ""
        self.qty = qty


class RiskManager(object):
    """
    Central risk manager shared across all symbols.

    NOTE:
    - This is NOT a complete OMS. It is a pragmatic overlay for the example script.
    - It assumes the script is the only active trader on the account (best effort).
    """

    def __init__(self, cfg, logger=None):
        self.cfg = cfg
        self._l = logger or logging.getLogger(__name__)

        # Portfolio state snapshots (best-effort, updated from events and periodic checks)
        self._pending_buy_notional = {}   # symbol -> notional reserved for open BUY orders
        self._open_positions = {}         # symbol -> dict(price, qty, entry_ts_utc)

        # Forced-exit / circuit breaker state
        self._forced_exits_by_symbol = defaultdict(int)
        self._symbol_disabled_until_utc = {}  # symbol -> ts (epoch seconds)

        # Daily loss kill-switch state
        self._start_equity = None
        self._start_equity_ts_utc = None
        self._kill_switch_triggered = False
        self._kill_switch_reason = ""

        # For informational PnL tracking (not used for kill-switch decision)
        self._realized_pnl_by_symbol = defaultdict(float)
        self._realized_pnl_total = 0.0

    # ---------------------------
    # Equity / kill-switch
    # ---------------------------

    def init_start_equity(self, api):
        """
        Capture starting equity at script start. Called once from main().
        """
        try:
            acct = api.get_account()
            self._start_equity = float(acct.equity)
            self._start_equity_ts_utc = time.time()
            self._l.info("risk: start equity captured: %.2f", self._start_equity)
        except Exception as e:
            # If this fails, we keep kill-switch disabled to avoid false triggers.
            self._l.error("risk: failed to fetch start equity; kill-switch will be disabled. error=%s", e)
            self._start_equity = None
            self._start_equity_ts_utc = None

    def check_kill_switch(self, api):
        """
        Returns True if kill switch should trigger. Once triggered, stays triggered.
        """
        if self._kill_switch_triggered:
            return True

        if self._start_equity is None:
            return False

        try:
            acct = api.get_account()
            equity = float(acct.equity)
        except Exception as e:
            self._l.error("risk: failed to fetch current equity for kill-switch check: %s", e)
            return False

        dd = self._start_equity - equity
        if dd >= self.cfg.max_daily_loss:
            self._kill_switch_triggered = True
            self._kill_switch_reason = "daily loss kill-switch: drawdown=%.2f >= max_daily_loss=%.2f" % (
                dd, self.cfg.max_daily_loss)
            self._l.error("risk: KILL SWITCH TRIGGERED: %s", self._kill_switch_reason)
            return True
        return False

    def kill_switch_reason(self):
        return self._kill_switch_reason

    def is_killed(self):
        return self._kill_switch_triggered

    def execute_kill_switch(self, api, fleet):
        """
        Best-effort emergency: cancel open orders, liquidate all positions, and disable further trading.
        """
        self._l.error("risk: executing kill-switch liquidation")

        # 1) Disable all symbols immediately
        for sym in list(fleet.keys()):
            self.disable_symbol(sym, reason="kill-switch", duration_minutes=24 * 60)

        # 2) Cancel orders (best effort)
        try:
            if hasattr(api, "cancel_all_orders"):
                api.cancel_all_orders()
            else:
                orders = api.list_orders(status="open")
                for o in orders:
                    try:
                        api.cancel_order(o.id)
                    except Exception:
                        pass
        except Exception as e:
            self._l.error("risk: failed to cancel open orders: %s", e)

        # 3) Liquidate positions (best effort)
        try:
            positions = api.list_positions()
        except Exception as e:
            self._l.error("risk: failed to list positions during kill-switch: %s", e)
            positions = []

        for p in positions:
            sym = getattr(p, "symbol", None)
            qty = getattr(p, "qty", None)
            if not sym or qty is None:
                continue
            try:
                api.submit_order(
                    symbol=sym,
                    side="sell",
                    type="market",
                    qty=qty,
                    time_in_force="day",
                )
                self._l.error("risk: kill-switch submitted market sell: %s qty=%s", sym, qty)
            except Exception as e:
                self._l.error("risk: kill-switch failed to submit market sell for %s: %s", sym, e)

        # 4) Notify algos (best effort)
        for sym, algo in fleet.items():
            try:
                algo.halt_trading(reason="kill-switch")
            except Exception:
                pass

    # ---------------------------
    # Symbol enable/disable
    # ---------------------------

    def is_symbol_enabled(self, symbol):
        until = self._symbol_disabled_until_utc.get(symbol)
        if until is None:
            return True
        # until == inf means disabled indefinitely
        if until == float('inf'):
            return False
        if time.time() >= until:
            # auto re-enable after cooldown
            del self._symbol_disabled_until_utc[symbol]
            return True
        until = self._symbol_disabled_until_utc.get(symbol)
        if until is None:
            return True
        if time.time() >= until:
            # auto re-enable after cooldown
            del self._symbol_disabled_until_utc[symbol]
            return True
        return False

    def disable_symbol(self, symbol, reason="", duration_minutes=0.0):
        # duration_minutes <= 0 means disabled indefinitely
        if duration_minutes and duration_minutes > 0:
            self._symbol_disabled_until_utc[symbol] = time.time() + float(duration_minutes) * 60.0
        else:
            self._symbol_disabled_until_utc[symbol] = float('inf')
        self._l.warning("risk: symbol disabled: %s reason=%s duration_minutes=%s",
                        symbol, reason, duration_minutes)

    def maybe_disable_after_forced_exit(self, symbol, reason=""):
        self._forced_exits_by_symbol[symbol] += 1
        n = self._forced_exits_by_symbol[symbol]
        if n >= self.cfg.symbol_max_forced_exits:
            # Disable for rest of day (approx). Without a full session calendar, use 24h.
            self.disable_symbol(symbol, reason="circuit breaker: forced exits=%d %s" % (n, reason), duration_minutes=24 * 60)
        elif self.cfg.forced_exit_cooldown_minutes > 0:
            # Temporary cooldown.
            self.disable_symbol(symbol, reason="cooldown after forced exit %s" % reason,
                                duration_minutes=self.cfg.forced_exit_cooldown_minutes)

    # ---------------------------
    # Exposure tracking
    # ---------------------------

    def sync_from_account(self, api):
        """
        Periodically sync open positions for more accurate portfolio exposure checks.
        This is best-effort and should not be called at high frequency.
        """
        try:
            positions = api.list_positions()
        except Exception as e:
            self._l.error("risk: sync_from_account failed to list positions: %s", e)
            return

        for p in positions:
            sym = getattr(p, "symbol", None)
            if not sym:
                continue
            try:
                avg = float(p.avg_entry_price)
                qty = float(p.qty)
            except Exception:
                continue

            if sym not in self._open_positions:
                self._open_positions[sym] = {
                    "price": avg,
                    "qty": qty,
                    "entry_ts_utc": time.time(),  # unknown after restart
                }

        # Remove symbols no longer held
        held = set([getattr(p, "symbol", None) for p in positions if getattr(p, "symbol", None)])
        for sym in list(self._open_positions.keys()):
            if sym not in held:
                del self._open_positions[sym]


    def sync_from_positions(self, positions):
        """
        Update open position snapshot from a pre-fetched list_positions() result.
        This avoids an extra REST call when the caller already fetched positions.

        positions: list of position objects (alpaca_trade_api)
        """
        try:
            held = set()
            for p in positions:
                sym = getattr(p, "symbol", None)
                if not sym:
                    continue
                held.add(sym)
                try:
                    avg = float(p.avg_entry_price)
                    qty = float(p.qty)
                except Exception:
                    continue

                if sym not in self._open_positions:
                    self._open_positions[sym] = {
                        "price": avg,
                        "qty": qty,
                        "entry_ts_utc": time.time(),  # unknown after restart
                    }
                else:
                    # refresh qty/price (avg may change with partial fills)
                    self._open_positions[sym]["price"] = avg
                    self._open_positions[sym]["qty"] = qty

            # Remove symbols no longer held
            for sym in list(self._open_positions.keys()):
                if sym not in held:
                    del self._open_positions[sym]
        except Exception as e:
            self._l.error("risk: sync_from_positions failed: %s", e)

    def note_pending_buy(self, symbol, notional):
        self._pending_buy_notional[symbol] = float(notional)

    def clear_pending_buy(self, symbol):
        if symbol in self._pending_buy_notional:
            del self._pending_buy_notional[symbol]

    def note_position_entry(self, symbol, qty, avg_price):
        self._open_positions[symbol] = {
            "price": float(avg_price),
            "qty": float(qty),
            "entry_ts_utc": time.time(),
        }

    def note_position_exit(self, symbol):
        if symbol in self._open_positions:
            del self._open_positions[symbol]

    def total_exposure_notional(self, api=None):
        """
        Compute total notional exposure = open positions (mark-to-market best-effort) + pending buy notional.
        If api is provided, uses last trade for mark; else uses entry price.
        """
        total = 0.0

        # pending buys: use reserved notional
        for sym, n in self._pending_buy_notional.items():
            total += float(n)

        # open positions: use last price if available, else entry price
        for sym, rec in self._open_positions.items():
            qty = float(rec.get("qty", 0.0))
            if qty <= 0:
                continue
            px = float(rec.get("price", 0.0))
            if api is not None:
                try:
                    trade = api.get_last_trade(sym)
                    px = float(trade.price)
                except Exception:
                    pass
            total += qty * px

        return total

    def count_open_positions(self):
        return len(self._open_positions)

    # ---------------------------
    # Market microstructure & volatility guards
    # ---------------------------

    def _safe_last_quote(self, api, symbol):
        """
        Return (bid, ask) as floats, or (None, None) if not available.
        Supports multiple SDK object shapes.
        """
        try:
            q = api.get_last_quote(symbol)
        except Exception:
            return (None, None)

        bid = None
        ask = None

        # alpaca_trade_api may return a dict-like or object-like result.
        for k in ("bidprice", "bid_price", "bp", "bidPrice", "bid"):
            if hasattr(q, k):
                try:
                    bid = float(getattr(q, k))
                    break
                except Exception:
                    pass
            if isinstance(q, dict) and k in q:
                try:
                    bid = float(q[k])
                    break
                except Exception:
                    pass

        for k in ("askprice", "ask_price", "ap", "askPrice", "ask"):
            if hasattr(q, k):
                try:
                    ask = float(getattr(q, k))
                    break
                except Exception:
                    pass
            if isinstance(q, dict) and k in q:
                try:
                    ask = float(q[k])
                    break
                except Exception:
                    pass

        return (bid, ask)

    def _spread_ok(self, api, symbol):
        if not self.cfg.enable_spread_guard:
            return (True, "")

        bid, ask = self._safe_last_quote(api, symbol)
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            # If quote isn't available, don't block (example script should still run).
            return (True, "spread guard skipped: no quote")

        spread = ask - bid
        mid = (ask + bid) / 2.0
        if mid <= 0:
            return (True, "spread guard skipped: invalid mid")

        spread_bps = (spread / mid) * 10000.0

        if self.cfg.max_spread_cents is not None and spread >= self.cfg.max_spread_cents:
            return (False, "spread too wide: %.4f >= max_spread_cents=%.4f (bid=%.4f ask=%.4f)" %
                    (spread, self.cfg.max_spread_cents, bid, ask))

        if spread_bps >= self.cfg.max_spread_bps:
            return (False, "spread too wide: %.2f bps >= max_spread_bps=%.2f (bid=%.4f ask=%.4f)" %
                    (spread_bps, self.cfg.max_spread_bps, bid, ask))

        return (True, "")

    def _volatility_ok(self, bars_df):
        if not self.cfg.enable_volatility_guard:
            return (True, "")

        if bars_df is None or len(bars_df) < 21:
            return (True, "")

        try:
            last = bars_df.iloc[-1]
            close = float(last["close"])
            high = float(last["high"])
            low = float(last["low"])
            if close > 0:
                bar_range_pct = (high - low) / close
                if bar_range_pct >= self.cfg.max_bar_range_pct:
                    return (False, "bar range too large: %.4f >= max_bar_range_pct=%.4f" %
                            (bar_range_pct, self.cfg.max_bar_range_pct))
        except Exception:
            # If bar parsing fails, don't block.
            return (True, "")

        # Return volatility on last 20 1-min closes
        try:
            closes = bars_df["close"].astype(float).values
            rets = pd.Series(closes).pct_change().dropna()
            recent = rets.iloc[-20:]
            if len(recent) >= 10:
                std = float(recent.std())
                if std >= self.cfg.max_return_std_pct:
                    return (False, "return std too high: %.4f >= max_return_std_pct=%.4f" %
                            (std, self.cfg.max_return_std_pct))
        except Exception:
            return (True, "")

        return (True, "")

    # ---------------------------
    # Pre-trade checks
    # ---------------------------

    def decide_buy_qty(self, api, symbol, desired_notional, price, bars_df):
        """
        Determine whether we can submit a new BUY order for this symbol, and the qty if so.
        Enforces:
          - kill-switch
          - symbol circuit breaker
          - max concurrent positions
          - max position notional
          - max total exposure
          - spread guard
          - volatility guard

        Returns RiskDecision(ok, reason, qty).
        """
        if self.is_killed():
            return RiskDecision(False, "kill-switch active")

        if not self.is_symbol_enabled(symbol):
            return RiskDecision(False, "symbol disabled")

        # Determine effective per-position notional cap
        max_pos_notional = self.cfg.max_position_notional
        if max_pos_notional is None:
            max_pos_notional = float(desired_notional)
        effective_notional = min(float(desired_notional), float(max_pos_notional))

        if effective_notional <= 0:
            return RiskDecision(False, "effective_notional <= 0")

        # Portfolio limits
        if self.count_open_positions() >= self.cfg.max_positions:
            return RiskDecision(False, "max_positions reached: %d" % self.cfg.max_positions)

        # Total exposure limit
        max_total = self.cfg.max_total_exposure
        if max_total is None:
            # By default, allow max_positions * effective_notional
            max_total = float(self.cfg.max_positions) * float(max_pos_notional)

        try:
            current_total = self.total_exposure_notional(api=api)
        except Exception:
            current_total = self.total_exposure_notional(api=None)

        if current_total + effective_notional > float(max_total):
            return RiskDecision(False, "max_total_exposure exceeded: current=%.2f + new=%.2f > max_total=%.2f" %
                                (current_total, effective_notional, float(max_total)))

        # Spread guard
        ok, reason = self._spread_ok(api, symbol)
        if not ok:
            return RiskDecision(False, reason)

        # Volatility guard
        ok, reason = self._volatility_ok(bars_df)
        if not ok:
            return RiskDecision(False, reason)

        if price is None or price <= 0:
            return RiskDecision(False, "invalid price")

        qty = int(float(effective_notional) / float(price))
        if qty <= 0:
            return RiskDecision(False, "qty computed as 0 (notional=%.2f price=%.4f)" %
                                (effective_notional, float(price)))

        return RiskDecision(True, "", qty=qty)

    # ---------------------------
    # Position exit checks (stop-loss / time-stop)
    # ---------------------------

    def should_force_exit(self, api, symbol):
        """
        Returns (True/False, reason) if a held position for symbol should be forcibly exited.
        Requires we have an entry record for the symbol; otherwise returns (False, "").
        """
        rec = self._open_positions.get(symbol)
        if rec is None:
            return (False, "")

        entry_px = float(rec.get("price", 0.0))
        entry_ts = float(rec.get("entry_ts_utc", 0.0))
        qty = float(rec.get("qty", 0.0))

        if entry_px <= 0 or qty <= 0:
            return (False, "")

        # Time-stop
        if self.cfg.time_stop_minutes and self.cfg.time_stop_minutes > 0:
            held_secs = time.time() - entry_ts
            if held_secs >= float(self.cfg.time_stop_minutes) * 60.0:
                return (True, "time-stop: held_secs=%.1f >= %.1f min" %
                        (held_secs, float(self.cfg.time_stop_minutes)))

        # Stop-loss (use last trade; quote would be better but we keep it simple)
        try:
            trade = api.get_last_trade(symbol)
            px = float(trade.price)
        except Exception:
            px = None

        if px is not None and px > 0:
            stop_px = entry_px * (1.0 - float(self.cfg.stop_loss_pct))
            if px <= stop_px:
                return (True, "stop-loss: price=%.4f <= stop_px=%.4f (entry=%.4f, stop_loss_pct=%.4f)" %
                        (px, stop_px, entry_px, float(self.cfg.stop_loss_pct)))

        return (False, "")

    # ---------------------------
    # Realized PnL tracking (informational + per-symbol breakers)
    # ---------------------------

    def note_realized_pnl(self, symbol, pnl):
        pnl = float(pnl)
        self._realized_pnl_by_symbol[symbol] += pnl
        self._realized_pnl_total += pnl

    def realized_pnl_total(self):
        return float(self._realized_pnl_total)

    def realized_pnl_symbol(self, symbol):
        return float(self._realized_pnl_by_symbol.get(symbol, 0.0))

