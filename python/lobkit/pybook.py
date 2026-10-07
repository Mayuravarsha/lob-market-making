"""A pure-Python limit order book with exactly the same semantics as the C++ one.

Why does this exist when we already have a fast C++ book?

1. FALLBACK. If someone clones the repo and does not build the C++ extension,
   everything still runs. Nothing kills a portfolio project faster than a
   reviewer hitting a compile error in the first 30 seconds.

2. DIFFERENTIAL TESTING. This is the real reason. Two independent
   implementations of the same specification, written in different languages
   with different data structures, fed the same million random events, must
   produce byte-identical output. If they diverge on event 418,332 you have
   found a bug that no hand-written unit test would ever have caught.
   This technique is how exchanges, database engines and compilers are
   validated. It is worth being able to explain in an interview.

3. It is the clearest possible statement of what the C++ code is doing, with
   none of the pool-allocator machinery in the way.

Semantics implemented (identical to the C++ engine):
  * price-time priority
  * trades print at the RESTING order's price
  * shrink-in-place keeps queue priority; grow or reprice loses it
  * GTC / IOC / FOK
  * duplicate order ids are rejected
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

BUY = 0
SELL = 1

GTC, IOC, FOK = 0, 1, 2


@dataclass
class PyTrade:
    ts: int
    price: int
    qty: int
    aggressor_id: int
    resting_id: int
    aggressor_side: int


@dataclass
class PyExecReport:
    order_id: int = 0
    filled_qty: int = 0
    resting_qty: int = 0
    accepted: bool = False
    n_trades: int = 0
    _px_num: int = 0

    @property
    def avg_fill_price(self) -> float:
        return self._px_num / self.filled_qty if self.filled_qty else 0.0


@dataclass
class _Order:
    id: int
    side: int
    price: int
    qty: int
    ts: int
    alive: bool = True


class PyOrderBook:
    """Dict-of-deques order book. O(L) top-of-book where L = number of levels.

    The C++ version keeps prices in a std::map so top-of-book is O(1). Here we
    just scan the dict keys, because L is small (tens of levels) and clarity
    beats speed in a reference implementation. This difference is deliberate
    and is exactly why we benchmark the C++ one instead of this one.
    """

    def __init__(self, pool_reserve: int = 0) -> None:  # signature-compatible
        self._bids: Dict[int, Deque[_Order]] = {}
        self._asks: Dict[int, Deque[_Order]] = {}
        self._orders: Dict[int, _Order] = {}
        self._qty: Dict[Tuple[int, int], int] = {}  # (side, price) -> total qty
        self._trades: List[PyTrade] = []
        self.total_trades = 0
        self.total_volume = 0

    # ------------------------------------------------------------------ reads
    @property
    def best_bid(self) -> Optional[int]:
        return max(self._bids) if self._bids else None

    @property
    def best_ask(self) -> Optional[int]:
        return min(self._asks) if self._asks else None

    @property
    def mid(self) -> Optional[float]:
        b, a = self.best_bid, self.best_ask
        return None if b is None or a is None else (b + a) / 2.0

    @property
    def spread(self) -> Optional[int]:
        b, a = self.best_bid, self.best_ask
        return None if b is None or a is None else a - b

    @property
    def microprice(self) -> Optional[float]:
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        bq = self._qty.get((BUY, b), 0)
        aq = self._qty.get((SELL, a), 0)
        if bq + aq == 0:
            return None
        return (b * aq + a * bq) / (bq + aq)

    @property
    def imbalance(self) -> Optional[float]:
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        bq = self._qty.get((BUY, b), 0)
        aq = self._qty.get((SELL, a), 0)
        if bq + aq == 0:
            return None
        return (bq - aq) / (bq + aq)

    @property
    def live_orders(self) -> int:
        return len(self._orders)

    @property
    def n_bid_levels(self) -> int:
        return len(self._bids)

    @property
    def n_ask_levels(self) -> int:
        return len(self._asks)

    def qty_at(self, side: int, price: int) -> int:
        return self._qty.get((side, price), 0)

    def contains(self, order_id: int) -> bool:
        return order_id in self._orders

    def depth(self, side: int, n_levels: int = 5) -> List[Tuple[int, int, int]]:
        book = self._bids if side == BUY else self._asks
        prices = sorted(book, reverse=(side == BUY))[:n_levels]
        return [(p, self._qty[(side, p)], len(book[p])) for p in prices]

    def queue_ahead(self, order_id: int) -> int:
        o = self._orders.get(order_id)
        if o is None:
            return -1
        book = self._bids if o.side == BUY else self._asks
        ahead = 0
        for other in book[o.price]:
            if other.id == order_id:
                return ahead
            ahead += other.qty
        return -1

    def trades(self) -> List[PyTrade]:
        return self._trades

    def trades_since(self, cursor: int) -> List[PyTrade]:
        return self._trades[cursor:]

    def n_trades_buffered(self) -> int:
        return len(self._trades)

    def clear_trades(self) -> None:
        self._trades.clear()

    def reserve_trades(self, n: int) -> None:  # no-op, for API compatibility
        pass

    # ----------------------------------------------------------------- writes
    def _book(self, side: int) -> Dict[int, Deque[_Order]]:
        return self._bids if side == BUY else self._asks

    def _crosses(self, aggressor_side: int, resting_px: int, limit_px: int) -> bool:
        return resting_px <= limit_px if aggressor_side == BUY else resting_px >= limit_px

    def _available(self, side: int, limit_px: Optional[int]) -> int:
        opp = self._asks if side == BUY else self._bids
        prices = sorted(opp) if side == BUY else sorted(opp, reverse=True)
        total = 0
        for px in prices:
            if limit_px is not None and not self._crosses(side, px, limit_px):
                break
            total += self._qty[(SELL if side == BUY else BUY, px)]
        return total

    def _match(self, aggressor_id: int, side: int, limit_px: Optional[int],
               qty: int, ts: int, rep: PyExecReport) -> int:
        opp_side = SELL if side == BUY else BUY
        opp = self._book(opp_side)
        filled = 0

        while qty > 0 and opp:
            px = min(opp) if side == BUY else max(opp)
            if limit_px is not None and not self._crosses(side, px, limit_px):
                break
            q = opp[px]
            while qty > 0 and q:
                resting = q[0]                     # head of FIFO = best priority
                take = min(qty, resting.qty)

                self._trades.append(
                    PyTrade(ts, px, take, aggressor_id, resting.id, side))
                rep._px_num += px * take
                qty -= take
                filled += take
                self.total_volume += take
                self.total_trades += 1
                self._qty[(opp_side, px)] -= take

                if take == resting.qty:
                    q.popleft()
                    resting.alive = False
                    del self._orders[resting.id]
                else:
                    resting.qty -= take
            if not q:
                del opp[px]
                self._qty.pop((opp_side, px), None)
        return filled

    def add_limit(self, order_id: int, side: int, price: int, qty: int,
                  ts: int, tif: int = GTC) -> PyExecReport:
        rep = PyExecReport(order_id=order_id)
        if qty <= 0 or order_id in self._orders:
            return rep
        if tif == FOK and self._available(side, price) < qty:
            return rep
        rep.accepted = True

        before = len(self._trades)
        filled = self._match(order_id, side, price, qty, ts, rep)
        rep.filled_qty = filled
        rep.n_trades = len(self._trades) - before

        remaining = qty - filled
        if remaining > 0 and tif == GTC:
            o = _Order(order_id, side, price, remaining, ts)
            self._book(side).setdefault(price, deque()).append(o)
            self._qty[(side, price)] = self._qty.get((side, price), 0) + remaining
            self._orders[order_id] = o
            rep.resting_qty = remaining
        return rep

    def add_market(self, order_id: int, side: int, qty: int, ts: int) -> PyExecReport:
        rep = PyExecReport(order_id=order_id)
        if qty <= 0:
            return rep
        rep.accepted = True
        before = len(self._trades)
        rep.filled_qty = self._match(order_id, side, None, qty, ts, rep)
        rep.n_trades = len(self._trades) - before
        return rep

    def cancel(self, order_id: int) -> bool:
        o = self._orders.pop(order_id, None)
        if o is None:
            return False
        book = self._book(o.side)
        q = book[o.price]
        q.remove(o)                                # O(k) here; O(1) in C++
        self._qty[(o.side, o.price)] -= o.qty
        if not q:
            del book[o.price]
            self._qty.pop((o.side, o.price), None)
        o.alive = False
        return True

    def modify(self, order_id: int, new_price: int, new_qty: int, ts: int) -> bool:
        o = self._orders.get(order_id)
        if o is None:
            return False
        if new_qty <= 0:
            return self.cancel(order_id)
        if new_price == o.price and new_qty == o.qty:
            return True
        if new_price == o.price and new_qty < o.qty:
            self._qty[(o.side, o.price)] -= (o.qty - new_qty)   # priority kept
            o.qty = new_qty
            return True
        side = o.side
        self.cancel(order_id)                                    # priority lost
        self.add_limit(order_id, side, new_price, new_qty, ts, GTC)
        return True

    def clear(self) -> None:
        self.__init__()

    def prefault(self) -> None:  # no-op, for API compatibility
        pass

    def replay_batch(self, types, ids, sides, prices, qtys, ts) -> int:
        before = self.total_trades
        for t, i, s, p, q, tt in zip(types, ids, sides, prices, qtys, ts):
            t = int(t)
            if t == 1:
                self.add_limit(int(i), int(s), int(p), int(q), int(tt))
            elif t == 2:
                self.cancel(int(i))
            elif t == 3:
                self.modify(int(i), int(p), int(q), int(tt))
            elif t == 4:
                self.add_market(int(i), int(s), int(q), int(tt))
        return self.total_trades - before
