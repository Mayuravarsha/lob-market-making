#!/usr/bin/env python3
"""Run one market-making backtest end to end and write the report + charts.

    python scripts/01_run_backtest.py --events 400000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import lobkit as lk                                        # noqa: E402
from lobkit import plots                                   # noqa: E402
from lobkit.backtest import Backtester, CostModel          # noqa: E402
from lobkit.metrics import format_report, summarize_performance  # noqa: E402
from lobkit.simulate import SimParams                      # noqa: E402
from lobkit.strategy import (AvellanedaStoikovMM, InventorySkewMM,  # noqa: E402
                             StrategyParams)

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "..", "results")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=400_000)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--strategy", choices=["skew", "as"], default="skew")
    ap.add_argument("--half-spread", type=float, default=1.0)
    ap.add_argument("--skew", type=float, default=0.6)
    ap.add_argument("--quote-size", type=int, default=50)
    ap.add_argument("--no-rebate", action="store_true",
                    help="set maker fee to zero to see PnL without the rebate")
    args = ap.parse_args()

    print(f"\norder book backend : {lk.BACKEND}")
    if lk.BACKEND == "python":
        print("  (pure-Python fallback -- build the C++ module for the real speed:")
        print("   cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j)")

    sp = SimParams(seed=args.seed)
    stp = StrategyParams(half_spread_ticks=args.half_spread,
                         skew_ticks=args.skew,
                         quote_size=args.quote_size)
    strat = InventorySkewMM(stp) if args.strategy == "skew" else AvellanedaStoikovMM(stp)
    costs = CostModel(maker_fee_per_share=0.0) if args.no_rebate else CostModel()

    print(f"strategy           : {strat.name}")
    print(f"events             : {args.events:,}\n")

    t0 = time.perf_counter()
    bt = Backtester(strat, sp, costs)
    result = bt.run(args.events, progress_every=max(1, args.events // 8))
    wall = time.perf_counter() - t0

    print(f"\nreplayed {result.n_events:,} events in {wall:.2f}s "
          f"({result.n_events/wall:,.0f} events/sec through the full backtest loop)")
    print(f"simulated market time: {result.sim_seconds/60:.1f} minutes")

    stats = summarize_performance(result)
    print(format_report(stats))

    print("""
  READ THIS BEFORE BELIEVING THE SHARPE
  -------------------------------------
  The annualised Sharpe above is computed by scaling 1-second PnL up to a year.
  That number is structurally inflated and should not be quoted on its own:

    * The simulated crowd does not react to us. Check `participation_pct` --
      if we are taking a large share of all volume, a real market would have
      repriced around us long ago.
    * There is one instrument, one venue, no queue-jumping competitors, no
      exchange outages, no latency jitter, and no position limits from a
      clearing firm.
    * Scaling by sqrt(time) assumes i.i.d. returns. High-frequency PnL is
      autocorrelated, which biases the annualised figure upward.

  Use scripts/02_experiments.py for the cross-run Sharpe, which treats each
  independent simulation as one observation. That is the number to quote.
""")

    os.makedirs(RESULTS, exist_ok=True)
    paths = plots.plot_backtest(result, stats, RESULTS,
                                prefix=f"backtest_{strat.name}")
    paths["book"] = plots.plot_book_snapshot(bt.book, RESULTS)
    for k, v in paths.items():
        print(f"  chart [{k}] -> {os.path.relpath(v, os.path.join(HERE, '..'))}")

    payload = {"config": vars(args), "backend": lk.BACKEND,
               "wall_seconds": wall,
               "events_per_second_full_loop": result.n_events / wall,
               "stats": stats}
    out = os.path.join(RESULTS, f"backtest_{strat.name}.json")
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"  stats -> {os.path.relpath(out, os.path.join(HERE, '..'))}\n")


if __name__ == "__main__":
    main()
