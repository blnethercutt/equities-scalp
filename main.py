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

from algo import ScalpAlgo

logger = logging.getLogger()

ALPACA_API_KEY = "<key_id>"
ALPACA_SECRET_KEY = "<secret_key>"


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

