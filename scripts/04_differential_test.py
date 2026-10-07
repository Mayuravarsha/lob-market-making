#!/usr/bin/env python3
"""Differential test: the C++ book and the Python book must agree, exactly.

WHAT DIFFERENTIAL TESTING IS
----------------------------
Write the same specification twice, independently, in two languages with two
different data structures. Feed both the same random inputs. Compare every
observable. Any disagreement is a bug in one of them -- and crucially, you did
not have to know in advance what the bug would look like.

This finds a class of defect that unit tests structurally cannot: the case you
never thought to write a test for. A million random events will exercise
combinations no human would enumerate -- a cancel of an order that was filled
in the same nanosecond, a modify that crosses the spread and partially fills
against three levels, a level that empties and is recreated at the same price.

WHAT WE COMPARE AFTER EVERY EVENT
---------------------------------
    best bid / best ask
    total size at the top of each side
    number of price levels each side
    cumulative trade count and traded volume
and, at checkpoints, the full 10-level depth ladder.

The first mismatch is printed with the exact event that caused it, so debugging
is a matter of reading one line rather than bisecting a million.

    python scripts/04_differential_test.py --events 200000
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import lobkit as lk                          # noqa: E402
from lobkit.pybook import PyOrderBook        # noqa: E402

BUY, SELL = 0, 1


def build_stream(n: int, seed: int = 20240501):
    """A nasty event stream: dense prices, duplicate cancels, modifies that cross."""
    rng = np.random.default_rng(seed)
    mid = 10_000
    events = []
    live: list = []
    nxt = 1
    for _ in range(n):
        r = rng.random()
        side = int(rng.integers(0, 2))
        if r < 0.45 or not live:
            # Narrow price band => levels constantly empty and are recreated,
            # and orders frequently cross the spread. Maximum stress.
            px = mid + int(rng.integers(-4, 5))
            qty = int(rng.integers(1, 400))
            events.append(("add", nxt, side, px, qty))
            live.append(nxt)
            nxt += 1
        elif r < 0.72:
            j = int(rng.integers(0, len(live)))
            events.append(("cancel", live[j], 0, 0, 0))
            if rng.random() < 0.85:              # sometimes cancel twice on purpose
                live[j] = live[-1]
                live.pop()
        elif r < 0.88:
            j = int(rng.integers(0, len(live)))
            px = mid + int(rng.integers(-4, 5))
            qty = int(rng.integers(1, 400))
            events.append(("modify", live[j], 0, px, qty))
        else:
            qty = int(rng.integers(1, 300))
            events.append(("market", nxt, side, 0, qty))
            nxt += 1
    return events


def state(book):
    bb, ba = book.best_bid, book.best_ask
    return (
        bb, ba,
        book.qty_at(BUY, bb) if bb is not None else 0,
        book.qty_at(SELL, ba) if ba is not None else 0,
        book.n_bid_levels, book.n_ask_levels,
        book.total_trades, book.total_volume,
    )


def apply(book, ev, i):
    kind, oid, side, px, qty = ev
    if kind == "add":
        book.add_limit(oid, side, px, qty, i)
    elif kind == "cancel":
        book.cancel(oid)
    elif kind == "modify":
        book.modify(oid, px, qty, i)
    else:
        book.add_market(oid, side, qty, i)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=200_000)
    ap.add_argument("--deep-check-every", type=int, default=500)
    args = ap.parse_args()

    if lk.BACKEND != "cpp":
        print("\nThe C++ module is not built, so there is nothing to compare against.")
        print("Build it with:  cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && "
              "cmake --build build -j\n")
        return

    print(f"\ngenerating {args.events:,} adversarial events...")
    events = build_stream(args.events)

    cpp = lk.OrderBook(1 << 18)
    py = PyOrderBook()

    labels = ("best_bid", "best_ask", "bid_top_qty", "ask_top_qty",
              "n_bid_levels", "n_ask_levels", "total_trades", "total_volume")
    mismatches = 0
    deep_checks = 0

    for i, ev in enumerate(events):
        apply(cpp, ev, i)
        apply(py, ev, i)

        a, b = state(cpp), state(py)
        if a != b:
            mismatches += 1
            if mismatches <= 3:
                print(f"\n  MISMATCH at event {i}: {ev}")
                for name, x, y in zip(labels, a, b):
                    if x != y:
                        print(f"      {name:<14} cpp={x!r:<12} python={y!r}")
            if mismatches > 20:
                print("\n  too many mismatches, stopping")
                break

        if i % args.deep_check_every == 0:
            deep_checks += 1
            if cpp.depth(BUY, 10) != py.depth(BUY, 10) or \
               cpp.depth(SELL, 10) != py.depth(SELL, 10):
                mismatches += 1
                print(f"\n  DEPTH LADDER MISMATCH at event {i}")
                print(f"      cpp bids: {cpp.depth(BUY, 10)}")
                print(f"      py  bids: {py.depth(BUY, 10)}")

        if cpp.n_trades_buffered() > 100_000:
            cpp.clear_trades()
            py.clear_trades()

    print(f"""
  events replayed        {len(events):>12,}
  full-ladder checks     {deep_checks:>12,}
  trades (C++)           {cpp.total_trades:>12,}
  trades (Python)        {py.total_trades:>12,}
  volume (C++)           {cpp.total_volume:>12,}
  volume (Python)        {py.total_volume:>12,}
  live orders (C++)      {cpp.live_orders:>12,}
  live orders (Python)   {py.live_orders:>12,}
  mismatches             {mismatches:>12,}
""")
    if mismatches == 0:
        print("  PASS -- two independent implementations agree on every "
              "observable, event by event.\n")
    else:
        print("  FAIL -- investigate the first mismatch printed above.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
