#!/usr/bin/env python3
"""Replay REAL NASDAQ Level-2 data and verify the reconstruction.

GETTING THE DATA (free, no account needed)
------------------------------------------
  1. Go to  https://lobsterdata.com/info/DataSamples.php
  2. Download any "Sample Files" zip (level 10 is ideal).
  3. Unzip both CSVs into this project's `data/` folder.

You should end up with a pair like

  data/AMZN_2012-06-21_34200000_57600000_message_10.csv
  data/AMZN_2012-06-21_34200000_57600000_orderbook_10.csv

Then just run this script; it finds the pair automatically.

WHAT IT DOES
------------
  1. Parses the message file into our engine's event vocabulary.
  2. Replays every event through the C++ book.
  3. At regular checkpoints, compares our reconstructed top of book against
     LOBSTER's own snapshot file.
  4. Measures replay throughput on real data.
  5. Runs the market maker over the real day.

Step 3 is the one that matters. "Replayed Level-2 data" is a claim; "verified my
book matched the exchange's own state at every checkpoint" is evidence.

    python scripts/05_lobster_real_data.py
    python scripts/05_lobster_real_data.py --message data/X_message_10.csv \\
                                           --orderbook data/X_orderbook_10.csv
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import lobkit as lk                       # noqa: E402
from lobkit import lobster                # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
RESULTS = os.path.join(HERE, "..", "results")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--message")
    ap.add_argument("--orderbook")
    ap.add_argument("--tick-size", type=float, default=0.01)
    ap.add_argument("--max-rows", type=int, default=None,
                    help="limit rows for a quick test")
    ap.add_argument("--check-every", type=int, default=500)
    args = ap.parse_args()

    msg, ob = args.message, args.orderbook
    if not msg:
        pairs = lobster.find_lobster_files(DATA)
        if not pairs:
            print(f"""
  No LOBSTER files found in {os.path.abspath(DATA)}

  Download a free sample day:
      https://lobsterdata.com/info/DataSamples.php
  and unzip the two CSVs (message + orderbook) into that folder.

  Everything else in this project runs without real data -- see
  scripts/01_run_backtest.py, which uses the built-in market simulator.
""")
            return
        msg, ob = pairs[0]

    print(f"\n  message file  : {os.path.basename(msg)}")
    print(f"  orderbook file: {os.path.basename(ob)}")

    t0 = time.perf_counter()
    data = lobster.load_messages(msg, args.tick_size, args.max_rows)
    t_parse = time.perf_counter() - t0
    n = len(data.types)
    print(f"""
  parsed {n:,} events in {t_parse:.2f}s
    ticker                   {data.ticker}
    date                     {data.date}
    hidden executions skipped {data.n_hidden_skipped:,}
    halts / auctions skipped  {data.n_halt_skipped:,}
""")

    # ------------------------------------------------- throughput on real data
    book = lk.OrderBook(1 << 20)
    book.reserve_trades(n // 4)
    if hasattr(book, "prefault"):
        book.prefault()
    t0 = time.perf_counter()
    n_trades = book.replay_batch(data.types, data.ids, data.sides,
                                 data.prices, data.qtys, data.ts)
    t_replay = time.perf_counter() - t0
    print(f"  REPLAY: {n:,} real events in {t_replay*1000:.1f} ms "
          f"= {n/t_replay:,.0f} events/sec, {n_trades:,} trades matched")
    print(f"          final book: bid {book.best_bid} / ask {book.best_ask}, "
          f"{book.live_orders:,} live orders")

    # ---------------------------------------------------------- verification
    print("\n  verifying against LOBSTER's own book snapshots...")
    v = lobster.validate_reconstruction(lk.OrderBook, data, ob,
                                        args.check_every, args.max_rows)
    print(f"""
    checkpoints compared     {v['checkpoints']:,}
    best bid price matched   {v['best_bid_match_pct']:.2f}%
    best ask price matched   {v['best_ask_match_pct']:.2f}%
    best bid SIZE matched    {v['bid_size_match_pct']:.2f}%
    best ask SIZE matched    {v['ask_size_match_pct']:.2f}%

  Prices should match essentially 100%. Sizes can differ slightly because
  hidden-order executions (message type 5) are skipped -- they consume
  liquidity that was never displayed, so replaying them would remove visible
  size that never actually traded.
""")

    os.makedirs(RESULTS, exist_ok=True)
    dest = os.path.join(RESULTS, "lobster_validation.json")
    with open(dest, "w") as f:
        json.dump({"ticker": data.ticker, "date": data.date, "n_events": n,
                   "replay_seconds": t_replay, "events_per_sec": n / t_replay,
                   "trades": n_trades, **v}, f, indent=2)
    print(f"  -> {os.path.relpath(dest, os.path.join(HERE, '..'))}\n")


if __name__ == "__main__":
    main()
