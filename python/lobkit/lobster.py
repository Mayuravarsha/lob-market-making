"""Loader for real Level-2 data in LOBSTER format.

WHERE TO GET THE DATA (free, no signup)
---------------------------------------
LOBSTER publishes free sample days of NASDAQ ITCH-reconstructed order books at

    https://lobsterdata.com/info/DataSamples.php

Download any sample (AAPL, AMZN, GOOG, INTC, MSFT are offered) and unzip it
into this project's `data/` folder. Each day gives you two CSV files:

    TICKER_YYYY-MM-DD_34200000_57600000_message_10.csv
    TICKER_YYYY-MM-DD_34200000_57600000_orderbook_10.csv

34200000 and 57600000 are milliseconds after midnight: 09:30:00 and 16:00:00,
i.e. the regular US trading session.

THE MESSAGE FILE -- one row per event, six columns, no header
-------------------------------------------------------------
    time       seconds after midnight, with nanosecond decimals
    type       1 = new limit order
               2 = partial cancellation (size reduced)
               3 = total deletion
               4 = execution of a visible limit order
               5 = execution of a HIDDEN order
               6 = cross / auction trade
               7 = trading halt
    order_id   0 for hidden executions and halts
    size       shares
    price      in units of $0.0001, so 1234500 means $123.45
    direction  1 = the RESTING order was a BUY (so an execution here means a
                   seller hit the bid);  -1 = the resting order was a SELL

THE SIGN CONVENTION IS THE #1 SOURCE OF BUGS. `direction` describes the side of
the order sitting in the BOOK, never the side of the aggressor. For a type-4
row with direction=1, somebody SOLD into a resting bid.

THE ORDERBOOK FILE -- the book state AFTER each message
--------------------------------------------------------
4*L columns for L levels:
    ask_price_1, ask_size_1, bid_price_1, bid_size_1, ask_price_2, ...
Prices again in $0.0001. Absent levels are padded with 9999999999 / -9999999999.

WHY WE CONVERT TO INTEGER TICKS
-------------------------------
LOBSTER prices are already integers (ten-thousandths of a dollar). Dividing by
100 gives whole cents for a normal US equity, which is exactly the tick grid our
C++ book wants. We assert that the division is exact so a sub-penny-priced
instrument fails loudly instead of silently rounding.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

BUY, SELL = 0, 1

# Our engine's event codes (see lob/types.hpp).
EV_ADD_LIMIT, EV_CANCEL, EV_MODIFY, EV_ADD_MARKET = 1, 2, 3, 4

MSG_COLS = ["time", "type", "order_id", "size", "price", "direction"]


@dataclass
class LobsterData:
    """A LOBSTER day, converted into our engine's event vocabulary."""
    types: np.ndarray      # uint8
    ids: np.ndarray        # uint64
    sides: np.ndarray      # uint8  (0 buy, 1 sell)
    prices: np.ndarray     # int64, integer ticks
    qtys: np.ndarray       # int64
    ts: np.ndarray         # int64, nanoseconds after midnight
    ticker: str = ""
    date: str = ""
    tick_size: float = 0.01
    n_hidden_skipped: int = 0
    n_halt_skipped: int = 0


def _read_messages(path: str) -> np.ndarray:
    return np.loadtxt(path, delimiter=",", ndmin=2)


def load_messages(message_csv: str, tick_size: float = 0.01,
                  max_rows: Optional[int] = None) -> LobsterData:
    """Convert a LOBSTER message file into engine events.

    MAPPING, AND WHY EACH CHOICE IS MADE
    ------------------------------------
    type 1 (new limit)        -> EV_ADD_LIMIT.
    type 2 (partial cancel)   -> EV_MODIFY with the REMAINING size. LOBSTER
                                 reports the size REMOVED, not the size left,
                                 so we have to track each order's live quantity
                                 ourselves. Getting this backwards silently
                                 corrupts every queue in the book.
    type 3 (total delete)     -> EV_CANCEL.
    type 4 (visible execution)-> EV_ADD_MARKET on the OPPOSITE side of
                                 `direction`, because the aggressor is the
                                 counterparty to the resting order. The engine
                                 then does the matching itself, which is a real
                                 consistency check: if our book disagrees with
                                 LOBSTER's, one of us is wrong.
    type 5 (hidden execution) -> SKIPPED. Hidden liquidity is by definition not
                                 in the visible book, so replaying it would
                                 consume displayed size that never actually
                                 traded. It is counted and reported instead.
    types 6, 7                -> SKIPPED (auction prints, halts).
    """
    raw = _read_messages(message_csv)
    if max_rows:
        raw = raw[:max_rows]

    t = raw[:, 0]
    mtype = raw[:, 1].astype(np.int64)
    oid = raw[:, 2].astype(np.int64)
    size = raw[:, 3].astype(np.int64)
    price_1e4 = raw[:, 4].astype(np.int64)
    direction = raw[:, 5].astype(np.int64)

    # $0.0001 units -> ticks. Must divide exactly.
    scale = int(round(tick_size / 0.0001))
    if np.any(price_1e4 % scale != 0):
        bad = int(np.sum(price_1e4 % scale != 0))
        raise ValueError(
            f"{bad} prices are not a whole number of {tick_size} ticks. "
            f"Pass the instrument's real tick_size.")
    ticks = price_1e4 // scale

    out_type, out_id, out_side, out_px, out_qty, out_ts = [], [], [], [], [], []
    live: dict = {}          # order_id -> remaining shares
    n_hidden = n_halt = 0

    for i in range(len(raw)):
        mt = mtype[i]
        ts_ns = int(round(t[i] * 1e9))
        # LOBSTER direction: +1 => the RESTING order is a BUY.
        resting_side = BUY if direction[i] == 1 else SELL

        if mt == 1:
            live[oid[i]] = size[i]
            out_type.append(EV_ADD_LIMIT); out_id.append(oid[i])
            out_side.append(resting_side); out_px.append(ticks[i])
            out_qty.append(size[i]); out_ts.append(ts_ns)

        elif mt == 2:
            remaining = live.get(oid[i], 0) - size[i]
            if remaining > 0:
                live[oid[i]] = remaining
                out_type.append(EV_MODIFY); out_id.append(oid[i])
                out_side.append(resting_side); out_px.append(ticks[i])
                out_qty.append(remaining); out_ts.append(ts_ns)
            else:
                live.pop(oid[i], None)
                out_type.append(EV_CANCEL); out_id.append(oid[i])
                out_side.append(resting_side); out_px.append(ticks[i])
                out_qty.append(0); out_ts.append(ts_ns)

        elif mt == 3:
            live.pop(oid[i], None)
            out_type.append(EV_CANCEL); out_id.append(oid[i])
            out_side.append(resting_side); out_px.append(ticks[i])
            out_qty.append(0); out_ts.append(ts_ns)

        elif mt == 4:
            # The aggressor is on the opposite side of the resting order.
            aggressor = SELL if resting_side == BUY else BUY
            rem = live.get(oid[i], 0) - size[i]
            if rem > 0:
                live[oid[i]] = rem
            else:
                live.pop(oid[i], None)
            out_type.append(EV_ADD_MARKET)
            out_id.append(900_000_000_000 + i)        # synthetic aggressor id
            out_side.append(aggressor); out_px.append(0)
            out_qty.append(size[i]); out_ts.append(ts_ns)

        elif mt == 5:
            n_hidden += 1
        else:
            n_halt += 1

    return LobsterData(
        types=np.asarray(out_type, dtype=np.uint8),
        ids=np.asarray(out_id, dtype=np.uint64),
        sides=np.asarray(out_side, dtype=np.uint8),
        prices=np.asarray(out_px, dtype=np.int64),
        qtys=np.asarray(out_qty, dtype=np.int64),
        ts=np.asarray(out_ts, dtype=np.int64),
        ticker=os.path.basename(message_csv).split("_")[0],
        date=(os.path.basename(message_csv).split("_")[1]
              if "_" in os.path.basename(message_csv) else ""),
        tick_size=tick_size,
        n_hidden_skipped=n_hidden,
        n_halt_skipped=n_halt,
    )


def load_orderbook(orderbook_csv: str, tick_size: float = 0.01,
                   max_rows: Optional[int] = None):
    """Load the reference book snapshots, for validating our reconstruction.

    Returns (ask_px, ask_sz, bid_px, bid_sz) arrays of shape (n_rows, n_levels),
    prices already converted to integer ticks.
    """
    raw = np.loadtxt(orderbook_csv, delimiter=",", ndmin=2)
    if max_rows:
        raw = raw[:max_rows]
    scale = int(round(tick_size / 0.0001))
    n_levels = raw.shape[1] // 4
    ask_px = raw[:, 0::4][:, :n_levels] / scale
    ask_sz = raw[:, 1::4][:, :n_levels]
    bid_px = raw[:, 2::4][:, :n_levels] / scale
    bid_sz = raw[:, 3::4][:, :n_levels]
    return ask_px, ask_sz, bid_px, bid_sz


def validate_reconstruction(book_factory, data: LobsterData,
                            orderbook_csv: str, check_every: int = 1000,
                            max_rows: Optional[int] = None) -> dict:
    """Replay the messages and compare our book against LOBSTER's snapshots.

    THIS IS THE MOST VALUABLE THING IN THE FILE. Anyone can write "replayed
    Level-2 data" on a CV. Being able to say "and I verified my reconstruction
    matched the exchange's own book state at 100% of checkpoints" is a
    different claim entirely, and it is the one that survives follow-up
    questions.

    Note that agreement will not be perfect out of the box: hidden executions
    (type 5) are skipped, and LOBSTER's snapshots are truncated to 10 levels.
    The function reports the mismatch rate rather than asserting, so you can
    see exactly how close you got.
    """
    ask_px, ask_sz, bid_px, bid_sz = load_orderbook(orderbook_csv,
                                                    data.tick_size, max_rows)
    book = book_factory(1 << 18)
    n_checked = n_bid_ok = n_ask_ok = n_bidsz_ok = n_asksz_ok = 0

    for i in range(len(data.types)):
        t = int(data.types[i])
        if t == EV_ADD_LIMIT:
            book.add_limit(int(data.ids[i]), int(data.sides[i]),
                           int(data.prices[i]), int(data.qtys[i]), int(data.ts[i]))
        elif t == EV_CANCEL:
            book.cancel(int(data.ids[i]))
        elif t == EV_MODIFY:
            book.modify(int(data.ids[i]), int(data.prices[i]),
                        int(data.qtys[i]), int(data.ts[i]))
        elif t == EV_ADD_MARKET:
            book.add_market(int(data.ids[i]), int(data.sides[i]),
                            int(data.qtys[i]), int(data.ts[i]))

        if i % check_every == 0 and i < len(bid_px):
            n_checked += 1
            bb, ba = book.best_bid, book.best_ask
            if bb is not None and abs(bb - bid_px[i, 0]) < 1e-9:
                n_bid_ok += 1
                if book.qty_at(BUY, bb) == bid_sz[i, 0]:
                    n_bidsz_ok += 1
            if ba is not None and abs(ba - ask_px[i, 0]) < 1e-9:
                n_ask_ok += 1
                if book.qty_at(SELL, ba) == ask_sz[i, 0]:
                    n_asksz_ok += 1

        if book.n_trades_buffered() > 200_000:
            book.clear_trades()

    return {
        "checkpoints": n_checked,
        "best_bid_match_pct": 100.0 * n_bid_ok / max(1, n_checked),
        "best_ask_match_pct": 100.0 * n_ask_ok / max(1, n_checked),
        "bid_size_match_pct": 100.0 * n_bidsz_ok / max(1, n_checked),
        "ask_size_match_pct": 100.0 * n_asksz_ok / max(1, n_checked),
        "hidden_executions_skipped": data.n_hidden_skipped,
        "halts_skipped": data.n_halt_skipped,
    }


def find_lobster_files(data_dir: str) -> List[Tuple[str, str]]:
    """Return (message_csv, orderbook_csv) pairs found in a directory."""
    if not os.path.isdir(data_dir):
        return []
    msgs = sorted(f for f in os.listdir(data_dir) if "message" in f and f.endswith(".csv"))
    pairs = []
    for m in msgs:
        ob = m.replace("message", "orderbook")
        if os.path.exists(os.path.join(data_dir, ob)):
            pairs.append((os.path.join(data_dir, m), os.path.join(data_dir, ob)))
    return pairs
