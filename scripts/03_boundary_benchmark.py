#!/usr/bin/env python3
"""How much does the Python <-> C++ boundary actually cost?

THE POINT OF THIS SCRIPT
------------------------
People write "exposed a C++ engine to Python with pybind11" on a CV and stop
there. The interesting engineering question is what that costs, and it is a lot:
a pybind11 call is roughly 100-300 ns, while our add_limit is 30-50 ns. Call the
engine once per event from Python and you spend most of your time marshalling
arguments, not matching orders.

We measure three things on the same workload:

  1. pure C++      -- replay N events inside one replay() call (from the
                      standalone benchmark binary; quoted for reference)
  2. batched       -- one replay_batch() call from Python with numpy arrays
  3. per-event     -- N separate add_limit/cancel calls from Python

The ratio between (2) and (3) is the number worth knowing, and it is why
`replay_batch` exists at all.

    python scripts/03_boundary_benchmark.py --events 300000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import lobkit as lk                                # noqa: E402
from lobkit.simulate import SimParams, generate_event_array   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "..", "results")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=300_000)
    args = ap.parse_args()
    n = args.events

    print(f"\nbackend: {lk.BACKEND}")
    print(f"building a {n:,}-event stream...")
    types, ids, sides, prices, qtys, ts = generate_event_array(n, SimParams())

    # ---------------------------------------------------------------- batched
    book = lk.OrderBook(1 << 20)
    book.reserve_trades(n // 4)
    if hasattr(book, "prefault"):
        book.prefault()
    t0 = time.perf_counter()
    n_trades = book.replay_batch(types, ids, sides, prices, qtys, ts)
    t_batch = time.perf_counter() - t0

    # -------------------------------------------------------------- per-event
    book2 = lk.OrderBook(1 << 20)
    book2.reserve_trades(n // 4)
    if hasattr(book2, "prefault"):
        book2.prefault()
    ty = types.tolist(); idl = ids.tolist(); sd = sides.tolist()
    px = prices.tolist(); qy = qtys.tolist(); tl = ts.tolist()

    add, cancel, market = book2.add_limit, book2.cancel, book2.add_market
    t0 = time.perf_counter()
    for i in range(n):
        k = ty[i]
        if k == 1:
            add(idl[i], sd[i], px[i], qy[i], tl[i])
        elif k == 2:
            cancel(idl[i])
        else:
            market(idl[i], sd[i], qy[i], tl[i])
    t_loop = time.perf_counter() - t0

    # ----------------------------------------------------------------- report
    print(f"""
  identical workload, {n:,} events, {n_trades:,} trades generated

  {'method':<44}{'seconds':>10}{'ns/event':>12}{'events/sec':>16}
  {'-'*82}
  {'batched replay_batch() -- 1 boundary crossing':<44}{t_batch:>10.4f}{t_batch/n*1e9:>12.0f}{n/t_batch:>16,.0f}
  {'per-event calls -- ' + f'{n:,} crossings':<44}{t_loop:>10.4f}{t_loop/n*1e9:>12.0f}{n/t_loop:>16,.0f}
  {'-'*82}
  batching is {t_loop/t_batch:.1f}x faster on exactly the same work.

  WHAT THIS MEANS IN PRACTICE
  ---------------------------
  Roughly {(t_loop - t_batch)/n*1e9:.0f} ns per event of the per-event timing is pure
  boundary overhead -- argument conversion, reference counting, and the GIL.
  The matching itself is the small part.

  So: keep tight loops on the C++ side and cross the boundary once per BATCH,
  not once per event. In this project that is why the throughput benchmark uses
  replay_batch, while the backtest -- which has to make a decision after every
  single event -- necessarily pays the per-event cost and is therefore bounded
  by Python, not by the order book.
""")

    if lk.BACKEND == "cpp":
        print("  For reference, the pure-C++ figure from the standalone benchmark")
        print("  (build/bench_order_book, BM_Replay_MixedFlow) has no boundary at all.\n")

    os.makedirs(RESULTS, exist_ok=True)
    dest = os.path.join(RESULTS, "boundary_benchmark.json")
    with open(dest, "w") as f:
        json.dump({
            "backend": lk.BACKEND, "n_events": n, "n_trades": n_trades,
            "batched_seconds": t_batch, "per_event_seconds": t_loop,
            "batched_events_per_sec": n / t_batch,
            "per_event_events_per_sec": n / t_loop,
            "speedup": t_loop / t_batch,
            "boundary_ns_per_event": (t_loop - t_batch) / n * 1e9,
        }, f, indent=2)
    print(f"  -> {os.path.relpath(dest, os.path.join(HERE, '..'))}\n")


if __name__ == "__main__":
    main()
