
"""Research CLI entrypoint scaffold (Step 2).

Step 2 scope:
- Provide a stable CLI surface and output directory convention.
- Do not implement replay execution yet (Step 3).

Usage (planned)
---------------
python research.py replay --symbols AAPL MSFT --bars AAPL=path.csv MSFT=path.csv --start ... --end ...
python research.py walkforward ...

Step 2 intentionally raises NotImplementedError for execution paths.
"""

import argparse
import json
import os
import time

from replay.data_source import HistoricalBarDataSource
from replay.sim_api import SimulatedAPI
from replay.broker import SimBroker
from replay.contracts import ReplayFriction


def _parse_kv_list(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise ValueError("expected KEY=VALUE, got: %s" % it)
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main():
    p = argparse.ArgumentParser(description="Equities-scalp research tools (scaffold)")
    sub = p.add_subparsers(dest="cmd")

    rp = sub.add_parser("replay", help="Historical replay (Step 3 will implement)")
    rp.add_argument("--symbols", nargs="+", required=True)
    rp.add_argument("--bars", nargs="+", required=True, help="symbol=path.csv for each symbol")
    rp.add_argument("--outdir", default="outputs")

    wf = sub.add_parser("walkforward", help="Walk-forward evaluation (Step 3+ will implement)")
    wf.add_argument("--outdir", default="outputs")

    args = p.parse_args()

    run_id = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(args.outdir, args.cmd or "run", run_id)
    if not os.path.exists(out):
        os.makedirs(out)

    # Persist run config for reproducibility
    with open(os.path.join(out, "run_config.json"), "w") as f:
        json.dump(vars(args), f, indent=2, sort_keys=True)

    if args.cmd == "replay":
        bars_map = _parse_kv_list(args.bars)
        ds = HistoricalBarDataSource(bars_map)
        api = SimulatedAPI(ds, broker=SimBroker(), friction=ReplayFriction(
            spread_bps=0.0,
            spread_cents_min=0.0,
            commission_per_share=0.0,
            notional_fee_rate=0.0,
            participation_rate=1.0,
            activation_latency_bars=0,
        ))
        raise NotImplementedError("Step 3 will implement replay execution.")
    elif args.cmd == "walkforward":
        raise NotImplementedError("Step 3+ will implement walk-forward execution.")
    else:
        p.print_help()


if __name__ == "__main__":
    main()
