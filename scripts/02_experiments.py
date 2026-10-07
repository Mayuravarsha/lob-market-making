#!/usr/bin/env python3
"""The experiments that turn a backtest into evidence.

Anyone can produce one equity curve. What makes a project credible is showing
that you understand WHY it works, by removing one ingredient at a time and
demonstrating the damage. Four studies here:

  A. ABLATIONS      Turn off inventory skew. Turn off informed flow. Turn off
                    the rebate. Each should move the results in a direction you
                    predicted before running it.
  B. PARAMETER SWEEP How PnL and inventory risk trade off against half-spread
                    and skew. There is a genuine optimum and it is not at the
                    edge of the grid.
  C. CROSS-RUN SHARPE  Independent simulations = independent observations. This
                    is the statistically defensible Sharpe, unlike the
                    intraday-scaled one.
  D. STRATEGY COMPARISON  Fixed-skew vs Avellaneda-Stoikov, same market.

    python scripts/02_experiments.py --events 150000 --runs 10
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import lobkit as lk                                        # noqa: E402
from lobkit import plots                                   # noqa: E402
from lobkit.backtest import Backtester, CostModel          # noqa: E402
from lobkit.metrics import (SECONDS_PER_TRADING_YEAR,      # noqa: E402
                            summarize_performance)
from lobkit.simulate import SimParams                      # noqa: E402
from lobkit.strategy import (AvellanedaStoikovMM, InventorySkewMM,  # noqa: E402
                             StrategyParams)

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "..", "results")


def run_one(strategy, sim_params, costs=None, events=150_000):
    bt = Backtester(strategy, sim_params, costs or CostModel())
    res = bt.run(events)
    return res, summarize_performance(res)


def hdr(title: str) -> None:
    print("\n" + "=" * 96)
    print(f"  {title}")
    print("=" * 96)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=150_000)
    ap.add_argument("--runs", type=int, default=10)
    args = ap.parse_args()
    os.makedirs(RESULTS, exist_ok=True)
    out: dict = {}

    E = args.events
    cols = (f"{'configuration':<38}{'PnL $':>10}{'Sharpe':>9}{'maxDD $':>9}"
            f"{'fills':>8}{'partic%':>8}{'cap c/sh':>10}{'advsel c/sh':>12}{'inv sd':>8}")

    # ======================================================================
    hdr("A.  ABLATIONS -- remove one ingredient at a time")
    # ======================================================================
    print("""
  Predictions to make BEFORE looking at the numbers:
    * no skew        -> inventory stdev explodes, drawdown grows, Sharpe falls
    * no informed flow -> adverse selection ~ 0, PnL looks unrealistically good
    * no rebate      -> PnL falls by roughly $0.002 x shares traded
    * wide spread    -> fewer fills, more edge each, less adverse selection
""")
    print(cols)
    print("-" * 96)

    ablations = [
        ("baseline", InventorySkewMM(StrategyParams()), SimParams(seed=11), None),
        ("no inventory skew (skew=0)",
         InventorySkewMM(StrategyParams(skew_ticks=0.0, taper=False)), SimParams(seed=11), None),
        ("no informed flow (kappa=0)",
         InventorySkewMM(StrategyParams()),
         SimParams(seed=11, informed_kappa=0.0, passive_kappa=0.0), None),
        ("no jumps",
         InventorySkewMM(StrategyParams()),
         SimParams(seed=11, jump_rate_per_s=0.0), None),
        ("no maker rebate",
         InventorySkewMM(StrategyParams()), SimParams(seed=11),
         CostModel(maker_fee_per_share=0.0)),
        ("wide quotes (half spread = 3 ticks)",
         InventorySkewMM(StrategyParams(half_spread_ticks=3.0)), SimParams(seed=11), None),
        ("mid anchor instead of microprice",
         InventorySkewMM(StrategyParams(use_microprice=False)), SimParams(seed=11), None),
        ("slow requoting (500 ms)",
         InventorySkewMM(StrategyParams(min_requote_interval_s=0.5)), SimParams(seed=11), None),
    ]
    abl_rows = []
    for label, strat, sp, cm in ablations:
        _, s = run_one(strat, sp, cm, E)
        abl_rows.append((label, s))
        print(f"{label:<38}{s['final_pnl']:>10,.0f}{s['sharpe_annualised']:>9,.0f}"
              f"{s['max_drawdown']:>9,.0f}{s['n_fills']:>8,.0f}"
              f"{s['participation_pct']:>8.1f}{s['spread_capture_per_share']*100:>10.2f}"
              f"{s['adverse_sel_1.0s_per_share']*100:>12.2f}"
              f"{s['stdev_inventory']:>8.0f}")
    out["ablations"] = {k: v for k, v in abl_rows}

    # ======================================================================
    hdr("B.  PARAMETER SWEEP -- half-spread x skew")
    # ======================================================================
    print("\n  PnL ($) / inventory stdev (shares), rows = half-spread ticks, cols = skew ticks\n")
    hs_grid = [0.5, 1.0, 2.0, 3.0]
    sk_grid = [0.0, 0.3, 0.6, 1.2]
    print("            " + "".join(f"skew={k:<12.1f}" for k in sk_grid))
    sweep = {}
    for hs in hs_grid:
        row = f"  hs={hs:<6.1f}"
        for sk in sk_grid:
            _, s = run_one(InventorySkewMM(StrategyParams(half_spread_ticks=hs, skew_ticks=sk)),
                           SimParams(seed=11), None, E)
            sweep[f"hs{hs}_skew{sk}"] = s
            row += f"{s['final_pnl']:>7,.0f}/{s['stdev_inventory']:<10.0f}"
        print(row)
    out["sweep"] = sweep

    # ======================================================================
    hdr("C.  CROSS-RUN SHARPE -- the statistically defensible one")
    # ======================================================================
    print(f"""
  Each of the {args.runs} runs uses a different random seed, so they are
  INDEPENDENT observations of the same strategy in the same market model.
  Treating one run as one "block" of trading time:

      Sharpe = mean(block PnL) / stdev(block PnL) * sqrt(blocks per year)

  This is honest in a way the intraday-scaled Sharpe is not, because it makes
  no i.i.d. assumption about second-to-second PnL -- it only assumes the runs
  are independent, which they are by construction.
""")
    pnls, hours_each = [], []
    for i in range(args.runs):
        res, s = run_one(InventorySkewMM(StrategyParams()), SimParams(seed=100 + i), None, E)
        pnls.append(s["final_pnl"])
        hours_each.append(s["sim_hours"])
        print(f"    run {i+1:>2}  seed={100+i}  PnL ${s['final_pnl']:>9,.0f}  "
              f"maxDD ${s['max_drawdown']:>7,.0f}  fills {s['n_fills']:>6,.0f}  "
              f"finalInv {s['final_inventory']:>6,.0f}")

    pnls = np.array(pnls)
    hrs = float(np.mean(hours_each))
    blocks_per_year = (SECONDS_PER_TRADING_YEAR / 3600.0) / hrs
    cross_sharpe = float(pnls.mean() / pnls.std(ddof=1) * np.sqrt(blocks_per_year))
    print(f"""
    block length          {hrs*60:>10.1f} minutes of market time
    mean PnL per block    ${pnls.mean():>9,.0f}
    stdev PnL per block   ${pnls.std(ddof=1):>9,.0f}
    worst block           ${pnls.min():>9,.0f}
    best block            ${pnls.max():>9,.0f}
    loss-making blocks    {int((pnls < 0).sum()):>10d} / {len(pnls)}
    ----------------------------------------------------------------
    CROSS-RUN SHARPE (annualised)   {cross_sharpe:>10,.1f}
    ----------------------------------------------------------------
  Still high, because the model has no regime changes and no competitor who
  undercuts our quote. State that limitation out loud; it is a strength to
  know where your own number comes from.""")
    out["cross_run"] = {"pnls": pnls.tolist(), "block_hours": hrs,
                        "cross_run_sharpe": cross_sharpe,
                        "mean": float(pnls.mean()), "std": float(pnls.std(ddof=1))}

    # ======================================================================
    hdr("D.  STRATEGY COMPARISON")
    # ======================================================================
    print(cols)
    print("-" * 96)
    comp_rows = []
    for label, strat in [
        ("fixed skew (practical)", InventorySkewMM(StrategyParams())),
        ("Avellaneda-Stoikov gamma=0.02", AvellanedaStoikovMM(StrategyParams(gamma=0.02))),
        ("Avellaneda-Stoikov gamma=0.06", AvellanedaStoikovMM(StrategyParams(gamma=0.06))),
        ("Avellaneda-Stoikov gamma=0.20", AvellanedaStoikovMM(StrategyParams(gamma=0.20))),
        ("no skew at all", InventorySkewMM(StrategyParams(skew_ticks=0.0, taper=False))),
    ]:
        _, s = run_one(strat, SimParams(seed=11), None, E)
        comp_rows.append((label, s))
        print(f"{label:<38}{s['final_pnl']:>10,.0f}{s['sharpe_annualised']:>9,.0f}"
              f"{s['max_drawdown']:>9,.0f}{s['n_fills']:>8,.0f}"
              f"{s['participation_pct']:>8.1f}{s['spread_capture_per_share']*100:>10.2f}"
              f"{s['adverse_sel_1.0s_per_share']*100:>12.2f}"
              f"{s['stdev_inventory']:>8.0f}")
    out["comparison"] = {k: v for k, v in comp_rows}

    p = plots.plot_strategy_comparison(comp_rows, RESULTS)
    p2 = plots.plot_strategy_comparison(abl_rows, RESULTS, name="ablations.png")
    print(f"\n  charts -> {os.path.relpath(p, os.path.join(HERE, '..'))}, "
          f"{os.path.relpath(p2, os.path.join(HERE, '..'))}")

    # ======================================================================
    hdr("E.  VOLATILITY SIGNATURE PLOT -- why you must not sample too fast")
    # ======================================================================
    print("""
  Estimate the volatility of the mid price at a range of sampling intervals.
  The observed mid is the efficient price PLUS microstructure noise (bid-ask
  bounce, one order flipping the touch). That noise does not grow with time,
  but squared returns get divided by a smaller dt as you sample faster, so the
  estimate diverges. Sample slowly enough and it converges on the truth.

  This is not academic: feeding the noisy estimate into Avellaneda-Stoikov made
  it quote 50 ticks wide and stop market making entirely.
""")
    sp_v = SimParams(seed=77)
    book_v = lk.OrderBook(1 << 18)
    from lobkit.simulate import FlowSimulator
    sim_v = FlowSimulator(book_v, sp_v)
    tt, mm = [], []
    for i in range(E):
        sim_v.step(); sim_v.sync_fills()
        if i % 5 == 0 and book_v.mid is not None:
            tt.append(sim_v.t); mm.append(float(book_v.mid))
        if book_v.n_trades_buffered() > 150_000:
            book_v.clear_trades(); sim_v.reset_trade_cursor()
    tt = np.asarray(tt); mm = np.asarray(mm)

    intervals = [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60]
    est = []
    print(f"    {'interval (s)':>14}{'estimated vol':>16}{'x true':>10}")
    for iv in intervals:
        grid = np.arange(tt[0], tt[-1], iv)
        idx = np.clip(np.searchsorted(tt, grid, side="right") - 1, 0, len(mm) - 1)
        r = np.diff(mm[idx])
        v = float(np.std(r) / np.sqrt(iv)) if r.size > 2 else float("nan")
        est.append(v)
        print(f"    {iv:>14}{v:>16.3f}{v/sp_v.fair_vol_ticks:>10.1f}")
    print(f"\n    true fair-value volatility = {sp_v.fair_vol_ticks:.2f} ticks/sqrt(s)")
    pv = plots.plot_vol_signature(intervals, est, sp_v.fair_vol_ticks, RESULTS)
    print(f"    chart -> {os.path.relpath(pv, os.path.join(HERE, '..'))}")
    out["vol_signature"] = {"intervals": intervals, "estimated": est,
                            "true": sp_v.fair_vol_ticks}

    dest = os.path.join(RESULTS, "experiments.json")
    with open(dest, "w") as f:
        json.dump(out, f, indent=2, default=float)
    print(f"  data   -> {os.path.relpath(dest, os.path.join(HERE, '..'))}\n")


if __name__ == "__main__":
    main()
