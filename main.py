import alpaca_trade_api as alpaca
import asyncio
import pandas as pd
import pytz
import sys
import logging

from alpaca_trade_api import Stream
from alpaca_trade_api.common import URL
from alpaca_trade_api.rest import TimeFrame

from risk import RiskConfig, RiskManager

logger = logging.getLogger()

ALPACA_API_KEY = "<key_id>"
ALPACA_SECRET_KEY = "<secret_key>"


class ScalpAlgo:
    def __init__(self, api, symbol, lot, risk=None):
        self._api = api
        self._symbol = symbol
        self._lot = lot
        self._risk = risk
        self._halted = False
        self._bars = []
        self._l = logger.getChild(self._symbol)

        now = pd.Timestamp.now(tz='America/New_York').floor('1min')
        market_open = now.replace(hour=9, minute=30)
        today = now.strftime('%Y-%m-%d')
        tomorrow = (now + pd.Timedelta('1day')).strftime('%Y-%m-%d')
        while 1:
            # at inception this results sometimes in api errors. this will work
            # around it. feel free to remove it once everything is stable
            try:
                data = api.get_bars(symbol, TimeFrame.Minute, today, tomorrow,
                                    adjustment='raw').df
                break
            except:
                # make sure we get bars
                pass
        bars = data[market_open:]
        self._bars = bars

        self._init_state()

        # Risk state bootstrap (best-effort)
        if self._risk is not None:
            try:
                if self._position is not None:
                    self._risk.note_position_entry(
                        self._symbol,
                        float(self._position.qty),
                        float(self._position.avg_entry_price),
                    )
                if self._order is not None and getattr(self._order, 'side', None) == 'buy':
                    # Reserve notional for any existing open BUY order on restart.
                    try:
                        qty = float(self._order.qty)
                        lp = float(self._order.limit_price) if self._order.limit_price is not None else 0.0
                        if qty > 0 and lp > 0:
                            self._risk.note_pending_buy(self._symbol, qty * lp)
                    except Exception:
                        pass
            except Exception as e:
                self._l.error(f'risk bootstrap failed: {e}')

    def _init_state(self):
        symbol = self._symbol
        order = [o for o in self._api.list_orders() if o.symbol == symbol]
        position = [p for p in self._api.list_positions()
                    if p.symbol == symbol]
        self._order = order[0] if len(order) > 0 else None
        self._position = position[0] if len(position) > 0 else None
        if self._position is not None:
            if self._order is None:
                self._state = 'TO_SELL'
            else:
                self._state = 'SELL_SUBMITTED'
                if self._order.side != 'sell':
                    self._l.warn(
                        f'state {self._state} mismatch order {self._order}')
        else:
            if self._order is None:
                self._state = 'TO_BUY'
            else:
                self._state = 'BUY_SUBMITTED'
                if self._order.side != 'buy':
                    self._l.warn(
                        f'state {self._state} mismatch order {self._order}')

    def _now(self):
        return pd.Timestamp.now(tz='America/New_York')

    def _outofmarket(self):
        return self._now().time() >= pd.Timestamp('15:55').time()

    def halt_trading(self, reason=''):
        """Disable further entries for this symbol (exits may still occur)."""
        self._halted = True
        self._l.error(f'trading halted: {reason}')

    def checkup(self, position):
        # self._l.info('periodic task')

        now = self._now()
        order = self._order
        if (order is not None and
            order.side == 'buy' and now -
                order.submitted_at.tz_convert(tz='America/New_York') > pd.Timedelta('2 min')):
            last_price = self._api.get_last_trade(self._symbol).price
            self._l.info(
                f'canceling missed buy order {order.id} at {order.limit_price} '
                f'(current price = {last_price})')
            self._cancel_order()

        # Per-position risk controls (stop-loss / time-stop)
        if self._risk is not None and self._position is not None:
            try:
                force, reason = self._risk.should_force_exit(self._api, self._symbol)
                if force:
                    self._l.error(f'forced exit triggered: {reason}')
                    self._force_exit_market(reason=reason)
            except Exception as e:
                self._l.error(f'risk exit check failed: {e}')

        # End-of-day liquidation
        if self._position is not None and self._outofmarket():
            # If an order is already working, cancel it first to avoid double-selling.
            if self._order is not None:
                self._cancel_order()
                self._order = None
            self._submit_sell(bailout=True)

    def _cancel_order(self):
        if self._order is not None:
            # Best-effort: if this is a BUY order, release reserved notional.
            if self._risk is not None and getattr(self._order, 'side', None) == 'buy':
                self._risk.clear_pending_buy(self._symbol)
            self._api.cancel_order(self._order.id)

    def _calc_buy_signal(self):
        mavg = self._bars.rolling(20).mean().close.values
        closes = self._bars.close.values
        if closes[-2] < mavg[-2] and closes[-1] > mavg[-1]:
            self._l.info(
                f'buy signal: closes[-2] {closes[-2]} < mavg[-2] {mavg[-2]} '
                f'closes[-1] {closes[-1]} > mavg[-1] {mavg[-1]}')
            return True
        else:
            self._l.info(
                f'closes[-2:] = {closes[-2:]}, mavg[-2:] = {mavg[-2:]}')
            return False

    def on_bar(self, bar):
        self._bars = self._bars.append(pd.DataFrame({
            'open': bar.open,
            'high': bar.high,
            'low': bar.low,
            'close': bar.close,
            'volume': bar.volume,
        }, index=[pd.Timestamp(bar.timestamp, tz=pytz.UTC)]))

        self._l.info(
            f'received bar start: {pd.Timestamp(bar.timestamp)}, close: {bar.close}, len(bars): {len(self._bars)}')
        if len(self._bars) < 21:
            return
        if self._outofmarket():
            return
        if self._halted:
            return
        if self._risk is not None:
            if self._risk.is_killed():
                return
            if not self._risk.is_symbol_enabled(self._symbol):
                return

        if self._state == 'TO_BUY':
            signal = self._calc_buy_signal()
            if signal:
                if self._risk is None:
                    self._submit_buy()
                else:
                    # Risk-gated sizing
                    try:
                        trade = self._api.get_last_trade(self._symbol)
                        px = float(trade.price)
                    except Exception as e:
                        self._l.error(f'failed to fetch last trade for sizing: {e}')
                        return

                    decision = self._risk.decide_buy_qty(
                        self._api,
                        self._symbol,
                        desired_notional=self._lot,
                        price=px,
                        bars_df=self._bars,
                    )
                    if not decision.ok:
                        self._l.info(f'skipping buy (risk): {decision.reason}')
                        return
                    self._submit_buy(qty=decision.qty, limit_price=px)

    def on_order_update(self, event, order):
        self._l.info(f'order update: {event} = {order}')

        # If we have a currently-tracked order, ignore updates for other orders.
        # This becomes important once we cancel/replace orders for forced exits.
        try:
            event_order_id = order.get('id') if isinstance(order, dict) else getattr(order, 'id', None)
            current_order_id = getattr(self._order, 'id', None) if self._order is not None else None
            if current_order_id is not None and event_order_id is not None and str(current_order_id) != str(event_order_id):
                self._l.info(f'ignoring order update for non-current order id={event_order_id}')
                return
        except Exception:
            pass
        if event == 'fill':
            # Release reserved notional for open BUY orders.
            if self._risk is not None and self._state == 'BUY_SUBMITTED':
                try:
                    self._risk.clear_pending_buy(self._symbol)
                except Exception:
                    pass

            self._order = None
            if self._state == 'BUY_SUBMITTED':
                self._position = self._api.get_position(self._symbol)
                if self._risk is not None:
                    try:
                        self._risk.clear_pending_buy(self._symbol)
                        self._risk.note_position_entry(
                            self._symbol,
                            float(self._position.qty),
                            float(self._position.avg_entry_price),
                        )
                    except Exception as e:
                        self._l.error(f'risk note_position_entry failed: {e}')
                self._transition('TO_SELL')
                self._submit_sell()
                return
            elif self._state == 'SELL_SUBMITTED':
                if self._risk is not None:
                    try:
                        # Best-effort realized PnL calculation.
                        rec = self._risk._open_positions.get(self._symbol)
                        entry_px = float(rec.get('price')) if rec is not None else None
                        exit_px = None
                        exit_qty = None
                        if isinstance(order, dict):
                            if order.get('filled_avg_price') is not None:
                                exit_px = float(order.get('filled_avg_price'))
                            if order.get('filled_qty') is not None:
                                exit_qty = float(order.get('filled_qty'))
                        if exit_px is None or exit_qty is None:
                            try:
                                od = self._api.get_order(order['id'])
                                if getattr(od, 'filled_avg_price', None) is not None:
                                    exit_px = float(od.filled_avg_price)
                                if getattr(od, 'filled_qty', None) is not None:
                                    exit_qty = float(od.filled_qty)
                            except Exception:
                                pass
                        if entry_px is not None and exit_px is not None and exit_qty is not None:
                            pnl = (float(exit_px) - float(entry_px)) * float(exit_qty)
                            self._risk.note_realized_pnl(self._symbol, pnl)
                        self._risk.note_position_exit(self._symbol)
                    except Exception as e:
                        self._l.error(f'risk note_position_exit failed: {e}')
                self._position = None
                self._transition('TO_BUY')
                return
        elif event == 'partial_fill':
            self._position = self._api.get_position(self._symbol)
            self._order = self._api.get_order(order['id'])
            return
        elif event in ('canceled', 'rejected'):
            if event == 'rejected':
                self._l.warn(f'order rejected: current order = {self._order}')
            # Release reserved notional for open BUY orders.
            if self._risk is not None and self._state == 'BUY_SUBMITTED':
                try:
                    self._risk.clear_pending_buy(self._symbol)
                except Exception:
                    pass

            self._order = None
            if self._state == 'BUY_SUBMITTED':
                if self._position is not None:
                    self._transition('TO_SELL')
                    self._submit_sell()
                else:
                    self._transition('TO_BUY')
            elif self._state == 'SELL_SUBMITTED':
                self._transition('TO_SELL')
                self._submit_sell(bailout=True)
            else:
                self._l.warn(f'unexpected state for {event}: {self._state}')

    def _submit_buy(self, qty=None, limit_price=None):
        trade = self._api.get_last_trade(self._symbol)
        px = float(trade.price)
        lp = float(limit_price) if limit_price is not None else px
        amount = int(qty) if qty is not None else int(self._lot / px)
        try:
            order = self._api.submit_order(
                symbol=self._symbol,
                side='buy',
                type='limit',
                qty=amount,
                time_in_force='day',
                limit_price=lp,
            )
        except Exception as e:
            self._l.info(e)
            self._transition('TO_BUY')
            return

        self._order = order
        if self._risk is not None:
            try:
                self._risk.note_pending_buy(self._symbol, float(amount) * float(lp))
            except Exception as e:
                self._l.error(f'risk note_pending_buy failed: {e}')
        self._l.info(f'submitted buy {order}')
        self._transition('BUY_SUBMITTED')


    def _force_exit_market(self, reason=''):
        """Best-effort forced exit: cancel any open order and submit a market sell."""
        try:
            if self._order is not None:
                self._cancel_order()
                self._order = None
        except Exception:
            pass

        # If we are in a position, exit at market.
        try:
            if self._position is not None:
                self._transition('TO_SELL')
                self._submit_sell(bailout=True)
                if self._risk is not None:
                    self._risk.maybe_disable_after_forced_exit(self._symbol, reason=reason)
        except Exception as e:
            self._l.error(f'force exit failed: {e}')
    def _submit_sell(self, bailout=False):
        params = dict(
            symbol=self._symbol,
            side='sell',
            qty=self._position.qty,
            time_in_force='day',
        )
        if bailout:
            params['type'] = 'market'
        else:
            current_price = float(
                self._api.get_last_trade(
                    self._symbol).price)
            cost_basis = float(self._position.avg_entry_price)
            limit_price = max(cost_basis + 0.01, current_price)
            params.update(dict(
                type='limit',
                limit_price=limit_price,
            ))
        try:
            order = self._api.submit_order(**params)
        except Exception as e:
            self._l.error(e)
            self._transition('TO_SELL')
            return

        self._order = order
        self._l.info(f'submitted sell {order}')
        self._transition('SELL_SUBMITTED')

    def _transition(self, new_state):
        self._l.info(f'transition from {self._state} to {new_state}')
        self._state = new_state


def main(args):
    stream = Stream(ALPACA_API_KEY,
                    ALPACA_SECRET_KEY,
                    base_url=URL('https://paper-api.alpaca.markets'),
                    data_feed='iex')  # <- replace to sip for PRO subscription
    api = alpaca.REST(key_id=ALPACA_API_KEY,
                    secret_key=ALPACA_SECRET_KEY,
                    base_url="https://paper-api.alpaca.markets")

    # Risk manager shared across symbols
    cfg = RiskConfig(
        max_positions=args.max_positions,
        max_position_notional=args.max_position_notional,
        max_total_exposure=args.max_total_exposure,
        max_daily_loss=args.max_daily_loss,
        stop_loss_pct=args.stop_loss_pct,
        time_stop_minutes=args.time_stop_minutes,
        max_spread_bps=args.max_spread_bps,
        max_spread_cents=args.max_spread_cents,
        max_bar_range_pct=args.max_bar_range_pct,
        max_return_std_pct=args.max_return_std_pct,
        symbol_max_forced_exits=args.symbol_max_forced_exits,
        forced_exit_cooldown_minutes=args.forced_exit_cooldown_minutes,
        enable_spread_guard=(not args.disable_spread_guard),
        enable_volatility_guard=(not args.disable_volatility_guard),
    )
    risk = RiskManager(cfg, logger=logger.getChild('risk'))
    risk.init_start_equity(api)

    fleet = {}
    symbols = args.symbols
    for symbol in symbols:
        algo = ScalpAlgo(api, symbol, lot=args.lot, risk=risk)
        fleet[symbol] = algo

    async def on_bars(data):
        if data.symbol in fleet:
            fleet[data.symbol].on_bar(data)

    for symbol in symbols:
        stream.subscribe_bars(on_bars, symbol)

    async def on_trade_updates(data):
        logger.info(f'trade_updates {data}')
        symbol = data.order['symbol']
        if symbol in fleet:
            fleet[symbol].on_order_update(data.event, data.order)

    stream.subscribe_trade_updates(on_trade_updates)

    async def periodic():
        while True:
            if not api.get_clock().is_open:
                logger.info('exit as market is not open')
                sys.exit(0)
            await asyncio.sleep(30)
            positions = api.list_positions()
            # Keep portfolio exposure snapshot reasonably fresh
            risk.sync_from_positions(positions)

            # Portfolio-level daily loss kill switch
            if risk.check_kill_switch(api):
                risk.execute_kill_switch(api, fleet)
                sys.exit(1)

            for symbol, algo in fleet.items():
                pos = [p for p in positions if p.symbol == symbol]
                algo.checkup(pos[0] if len(pos) > 0 else None)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.gather(
        stream._run_forever(),
        periodic(),
    ))
    loop.close()


if __name__ == '__main__':
    import argparse

    fmt = '%(asctime)s:%(filename)s:%(lineno)d:%(levelname)s:%(name)s:%(message)s'
    logging.basicConfig(level=logging.INFO, format=fmt)
    fh = logging.FileHandler('console.log')
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(fmt))
    logger.addHandler(fh)

    parser = argparse.ArgumentParser()
    parser.add_argument('symbols', nargs='+')
    parser.add_argument('--lot', type=float, default=2000)

    # Risk controls (portfolio + per-position)
    parser.add_argument('--max-positions', type=int, default=3,
                        help='Max concurrent open positions across all symbols.')
    parser.add_argument('--max-position-notional', type=float, default=None,
                        help='Max notional (USD) for any single new position. Default: --lot')
    parser.add_argument('--max-total-exposure', type=float, default=None,
                        help='Max total exposure (USD) across positions + pending buys. Default: max_positions * max_position_notional')
    parser.add_argument('--max-daily-loss', type=float, default=100.0,
                        help='Daily loss kill-switch (USD) based on account equity drawdown from script start.')

    parser.add_argument('--stop-loss-pct', type=float, default=0.003,
                        help='Stop-loss percentage vs entry price (e.g., 0.003 = 0.3%%).')
    parser.add_argument('--time-stop-minutes', type=float, default=10.0,
                        help='Maximum holding time in minutes before forced market exit.')

    parser.add_argument('--max-spread-bps', type=float, default=25.0,
                        help='Spread guard: max bid/ask spread in basis points of mid-price.')
    parser.add_argument('--max-spread-cents', type=float, default=None,
                        help='Spread guard: max absolute spread in dollars (overrides if set).')

    parser.add_argument('--max-bar-range-pct', type=float, default=0.01,
                        help='Volatility guard: max (high-low)/close for the latest 1-min bar.')
    parser.add_argument('--max-return-std-pct', type=float, default=0.01,
                        help='Volatility guard: max std dev of last 20 1-min returns.')

    parser.add_argument('--symbol-max-forced-exits', type=int, default=2,
                        help='Circuit breaker: disable a symbol after N forced exits.')
    parser.add_argument('--forced-exit-cooldown-minutes', type=float, default=0.0,
                        help='Optional cooldown per symbol after a forced exit (minutes).')

    parser.add_argument('--disable-spread-guard', action='store_true',
                        help='Disable spread-based entry guard.')
    parser.add_argument('--disable-volatility-guard', action='store_true',
                        help='Disable volatility-based entry guard.')

    main(parser.parse_args())


