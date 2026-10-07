"""A realistic order-flow simulator for the limit order book.

WHY SIMULATE AT ALL?
--------------------
Free Level-2 data is scarce (see lobkit/lobster.py for the real-data path). But
there is a second, better reason: with a simulator you KNOW the ground truth.
You know the true fair value at every instant, you know exactly which trades
were informed, and you can turn individual effects on and off. That lets you
prove your backtester is measuring what you think it is measuring before you
ever point it at real data.

THE MODEL
---------
The backbone is the Cont-Stoikov-Talreja (2010) stochastic order-book model. It
treats the book as a system of queues driven by three Poisson processes:

  1. LIMIT ORDERS arrive `i` ticks away from the opposite best quote at rate
         lambda(i) = lambda0 / i**alpha
     Most activity happens near the touch and decays with distance -- this
     power law is one of the most robust empirical facts about real books.

  2. MARKET ORDERS arrive at rate mu and consume the best quote.

  3. CANCELLATIONS at a level occur at rate theta * q, proportional to the size
     resting there. Intuition: every resting share has its own constant hazard
     of being pulled, so a bigger queue sheds size faster in absolute terms.

Everything is a competing exponential clock, simulated with the Gillespie
algorithm: total rate -> draw the waiting time -> draw which event fired.

THE PART THAT MATTERS MOST: ADVERSE SELECTION
---------------------------------------------
Pure CST flow is symmetric and the mid mean-reverts. A market maker in that
world prints money, and any backtest built on it is a fantasy that an
interviewer will demolish in one question.

Real market makers lose money to INFORMED traders: someone who knows the price
is about to go up buys from you, and moments later your inventory is worth less
than you paid. That loss is called adverse selection and it is the single
largest cost in market making.

So this simulator carries a hidden fair value F that follows a random walk.
Order flow is tilted toward it:

    P(next market order is a buy) = sigmoid(kappa * (F - mid))

When F drifts above the mid, buyers show up, they lift your offer, and then the
mid follows F upward -- leaving you short at a bad price. That is adverse
selection, generated endogenously rather than bolted on. The `markout` metrics
in metrics.py measure exactly how much of it you are suffering.

Set `informed_kappa=0.0` to switch it off and watch the strategy's Sharpe
become absurd. That comparison is a good thing to show.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

BUY, SELL = 0, 1


# ---------------------------------------------------------------------------
@dataclass
class SimParams:
    """Every knob, with units, so nothing is a magic number."""

    n_levels: int = 8          # how many ticks deep flow arrives
    lambda0: float = 12.0      # limit orders/sec at distance 1 tick
    alpha: float = 0.9         # decay exponent of lambda(i) = lambda0 / i^alpha
    mu: float = 10.0           # market orders/sec, EACH side (so 2*mu total)
    # Cancellation hazard, per resting SHARE per second. One cancel event
    # removes one whole order (mean `order_size_mean` shares), so the book is
    # in flow balance when
    #     theta * Q  ~=  (limit_arrival_rate * order_size - market_flow) / order_size
    # Getting this wrong by an order of magnitude either freezes the book solid
    # or shreds it into a one-tick-deep mess, and every downstream statistic
    # then measures the bug rather than the strategy. Default is solved for
    # roughly 500 shares per level of steady-state depth.
    theta: float = 0.0140
    order_size_mean: float = 120.0   # mean size of an incoming limit order
    market_size_mean: float = 180.0  # mean size of an incoming market order

    # --- the informed-flow / adverse-selection channel -----------------------
    # fair_vol_ticks is calibrated, not guessed. A $100 stock at 20% annualised
    # vol moves 20%/sqrt(252) = 1.26% = $1.26 = 126 ticks over a 6.5h (23,400s)
    # session, so the per-sqrt-second figure is 126/sqrt(23400) ~= 0.8 ticks.
    # We use a slightly calmer 0.6.
    fair_vol_ticks: float = 0.60   # stdev of the hidden fair value, ticks/sec^0.5
    informed_kappa: float = 5.0    # how strongly market orders chase fair value
    passive_kappa: float = 1.75    # how strongly limit orders chase fair value

    # --- jumps: the thing that actually hurts a market maker -----------------
    # A pure diffusion is far too kind. Real losses come from discrete news: the
    # price gaps, every quote you have resting on the wrong side gets lifted at
    # the old price, and you are instantly holding a losing position. Jumps are
    # what give the PnL distribution its fat left tail -- without them a
    # market-making backtest reports a Sharpe in the hundreds and means nothing.
    jump_rate_per_s: float = 1.0 / 240.0   # ~one news event every 4 minutes
    jump_size_ticks: float = 8.0           # stdev of the jump, in ticks

    # --- initial state -------------------------------------------------------
    init_mid: int = 10000      # in ticks; 10000 ticks * $0.01 = $100.00
    init_spread: int = 2       # ticks
    init_depth: int = 400      # shares seeded at each level at t=0
    tick_size: float = 0.01    # dollars per tick, used only for reporting

    seed: int = 7


class _RandBuf:
    """Draw random numbers in blocks.

    numpy has roughly a microsecond of call overhead. In a loop that runs a
    million times and needs three random numbers per iteration, that overhead
    is most of the runtime. Filling a 64k buffer once and handing out slices
    makes the simulator about 5x faster with no change in the maths.
    """

    def __init__(self, rng: np.random.Generator, n: int = 1 << 16) -> None:
        self.rng, self.n = rng, n
        self._u = rng.random(n)
        self._z = rng.standard_normal(n)
        self._e = rng.exponential(size=n)
        self.i = 0

    def _refill(self) -> None:
        self._u = self.rng.random(self.n)
        self._z = self.rng.standard_normal(self.n)
        self._e = self.rng.exponential(size=self.n)
        self.i = 0

    def next(self) -> Tuple[float, float, float]:
        if self.i >= self.n:
            self._refill()
        i = self.i
        self.i += 1
        return self._u[i], self._z[i], self._e[i]


# ---------------------------------------------------------------------------
class FlowSimulator:
    """Drives background order flow through a book, one event at a time.

    The book is shared with the strategy: the market maker's own orders sit in
    the same queues as the simulated crowd, which is what makes queue position
    (and therefore fill probability) realistic.

    The simulator tracks its OWN orders separately so it never cancels one of
    the strategy's quotes, and so cancellation rates depend on background size
    rather than on total size.
    """

    def __init__(self, book, params: Optional[SimParams] = None,
                 first_order_id: int = 1_000_000) -> None:
        self.p = params or SimParams()
        self.book = book
        self.rng = np.random.default_rng(self.p.seed)
        self.rb = _RandBuf(self.rng)

        self.t = 0.0                     # simulation clock, seconds
        self.next_id = first_order_id
        self.fair = float(self.p.init_mid)   # hidden fair value, in ticks

        # Background-order bookkeeping.
        self.oq: Dict[int, int] = {}                     # order id -> qty left
        self.okey: Dict[int, Tuple[int, int]] = {}       # order id -> (side, price)
        self.level_ids: Dict[Tuple[int, int], List[int]] = {}
        self.level_qty: Dict[Tuple[int, int], int] = {}

        self._trade_cursor = 0
        self.n_events = 0
        self.n_jumps = 0

        # lambda(i) lookup table, computed once.
        K = self.p.n_levels
        self._lam = np.array([self.p.lambda0 / ((i + 1) ** self.p.alpha)
                              for i in range(K)])
        self._rates = np.zeros(4 * K + 2)
        self._cum = np.zeros(4 * K + 2)   # reused buffer: no allocation per step

        self._seed_book()

    # ------------------------------------------------------------------ setup
    def _seed_book(self) -> None:
        p = self.p
        half = p.init_spread // 2
        b0 = p.init_mid - max(1, half)
        a0 = p.init_mid + max(1, p.init_spread - half)
        for i in range(p.n_levels):
            self._place(BUY, b0 - i, p.init_depth)
            self._place(SELL, a0 + i, p.init_depth)

    # -------------------------------------------------------------- internals
    def _place(self, side: int, price: int, qty: int) -> int:
        oid = self.next_id
        self.next_id += 1
        rep = self.book.add_limit(oid, side, int(price), int(qty),
                                  int(self.t * 1e9))
        resting = int(rep.resting_qty)
        if resting > 0:
            key = (side, int(price))
            self.oq[oid] = resting
            self.okey[oid] = key
            self.level_ids.setdefault(key, []).append(oid)
            self.level_qty[key] = self.level_qty.get(key, 0) + resting
        return oid

    def _forget(self, oid: int) -> None:
        key = self.okey.pop(oid, None)
        if key is None:
            return
        self.oq.pop(oid, None)
        ids = self.level_ids.get(key)
        if ids:
            try:
                ids.remove(oid)
            except ValueError:
                pass
            if not ids:
                self.level_ids.pop(key, None)

    def sync_fills(self) -> List:
        """Consume new trades and keep our background bookkeeping honest.

        Returns the list of new trades so the backtester can inspect them for
        its own fills. Must be called after every event.

        Uses trades_since(cursor), NOT trades(). The latter copies the entire
        trade history across the C++/Python boundary on every call, which makes
        the backtest quadratic in the number of events -- a mistake that is
        completely invisible until you profile it.
        """
        new = self.book.trades_since(self._trade_cursor)
        self._trade_cursor += len(new)
        for tr in new:
            rid = tr.resting_id
            left = self.oq.get(rid)
            if left is None:
                continue                      # it was one of the strategy's orders
            key = self.okey[rid]
            self.level_qty[key] = self.level_qty.get(key, 0) - tr.qty
            left -= tr.qty
            if left <= 0:
                self._forget(rid)
            else:
                self.oq[rid] = left
        return new

    def reset_trade_cursor(self) -> None:
        """Call after book.clear_trades() so indices stay aligned."""
        self._trade_cursor = 0

    def _size(self, mean: float, u: float) -> int:
        # Order sizes in real markets are heavily right-skewed with clusters at
        # round lots. Exponential + round-to-10 is a cheap, decent stand-in.
        return max(10, int(round(-mean * math.log(max(u, 1e-12)) / 10.0)) * 10)

    # ------------------------------------------------------------------- step
    def step(self) -> Optional[dict]:
        """Advance the simulation by one event. Returns a small event record."""
        p = self.p
        K = p.n_levels
        book = self.book

        b = book.best_bid
        a = book.best_ask
        # Keep the book two-sided. A one-sided book is not a market and every
        # metric derived from the mid becomes undefined.
        if b is None:
            b = int(round(self.fair)) - 1
            self._place(BUY, b, p.init_depth)
        if a is None:
            a = int(round(self.fair)) + 1
            self._place(SELL, a, p.init_depth)
        if a <= b:                                   # degenerate, should not happen
            a = b + 1
            self._place(SELL, a, p.init_depth)

        mid = 0.5 * (a + b)

        # ---- informed-flow tilt --------------------------------------------
        # sigmoid(kappa * (fair - mid)): 0.5 when fair == mid, ->1 when the
        # hidden value is far above the visible mid.
        edge = self.fair - mid
        s_mkt = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, p.informed_kappa * edge))))
        s_lim = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, p.passive_kappa * edge))))

        r = self._rates
        # limit buys, distance i+1 below the best ask
        r[0:K] = self._lam * (2.0 * s_lim)
        # limit sells, distance i+1 above the best bid
        r[K:2 * K] = self._lam * (2.0 * (1.0 - s_lim))
        # market buy / market sell
        r[2 * K] = p.mu * 2.0 * s_mkt
        r[2 * K + 1] = p.mu * 2.0 * (1.0 - s_mkt)
        # cancellations: rate proportional to BACKGROUND size resting there
        lq = self.level_qty
        for i in range(K):
            r[2 * K + 2 + i] = p.theta * lq.get((BUY, a - 1 - i), 0)
            r[3 * K + 2 + i] = p.theta * lq.get((SELL, b + 1 + i), 0)

        # cumsum into a preallocated buffer: one pass, zero allocation.
        np.cumsum(r, out=self._cum)
        total = float(self._cum[-1])
        if total <= 0.0:
            return None

        u, z, e = self.rb.next()
        dt = e / total                       # exponential waiting time
        self.t += dt
        # Hidden fair value diffuses. sqrt(dt) is the scaling of Brownian motion.
        self.fair += p.fair_vol_ticks * math.sqrt(dt) * z

        # ...and occasionally jumps. Probability of at least one Poisson arrival
        # in an interval dt is 1 - exp(-rate*dt).
        if p.jump_rate_per_s > 0.0:
            u_j, z_j, _ = self.rb.next()
            if u_j < -math.expm1(-p.jump_rate_per_s * dt):
                self.fair += p.jump_size_ticks * z_j
                self.n_jumps += 1

        # Pick which clock fired, proportional to its rate.
        k = int(np.searchsorted(self._cum, u * total))
        k = min(k, len(r) - 1)

        u2, _, _ = self.rb.next()
        ts = int(self.t * 1e9)
        ev: dict

        if k < K:                                            # limit BUY
            px = a - 1 - k
            qty = self._size(p.order_size_mean, u2)
            self._place(BUY, px, qty)
            ev = {"type": "limit", "side": BUY, "price": px, "qty": qty}

        elif k < 2 * K:                                      # limit SELL
            px = b + 1 + (k - K)
            qty = self._size(p.order_size_mean, u2)
            self._place(SELL, px, qty)
            ev = {"type": "limit", "side": SELL, "price": px, "qty": qty}

        elif k == 2 * K:                                     # market BUY
            qty = self._size(p.market_size_mean, u2)
            self.next_id += 1
            book.add_market(self.next_id, BUY, qty, ts)
            ev = {"type": "market", "side": BUY, "price": a, "qty": qty}

        elif k == 2 * K + 1:                                 # market SELL
            qty = self._size(p.market_size_mean, u2)
            self.next_id += 1
            book.add_market(self.next_id, SELL, qty, ts)
            ev = {"type": "market", "side": SELL, "price": b, "qty": qty}

        elif k < 3 * K + 2:                                  # cancel a BUY
            px = a - 1 - (k - 2 * K - 2)
            ev = self._cancel_random(BUY, px, u2)

        else:                                                # cancel a SELL
            px = b + 1 + (k - 3 * K - 2)
            ev = self._cancel_random(SELL, px, u2)

        self.n_events += 1
        ev["t"] = self.t
        ev["ts"] = ts
        return ev

    def _cancel_random(self, side: int, price: int, u: float) -> dict:
        key = (side, int(price))
        ids = self.level_ids.get(key)
        if not ids:
            return {"type": "noop", "side": side, "price": price, "qty": 0}
        j = min(int(u * len(ids)), len(ids) - 1)
        oid = ids[j]
        qty = self.oq.get(oid, 0)
        if self.book.cancel(oid):
            self.level_qty[key] = self.level_qty.get(key, 0) - qty
            self._forget(oid)
            return {"type": "cancel", "side": side, "price": price, "qty": qty}
        self._forget(oid)
        return {"type": "noop", "side": side, "price": price, "qty": 0}


# ---------------------------------------------------------------------------
def generate_event_array(n: int, params: Optional[SimParams] = None):
    """Produce a flat event array for throughput benchmarking of the C++ book.

    This is deliberately NOT the queue-reactive model above: it is a cheap
    synthetic stream whose only job is to exercise the engine at speed. Use
    FlowSimulator for anything where the market dynamics matter.

    Returns six numpy arrays ready for OrderBook.replay_batch.
    """
    p = params or SimParams()
    rng = np.random.default_rng(p.seed)

    types = np.empty(n, dtype=np.uint8)
    ids = np.empty(n, dtype=np.uint64)
    sides = np.empty(n, dtype=np.uint8)
    prices = np.zeros(n, dtype=np.int64)
    qtys = np.empty(n, dtype=np.int64)
    ts = np.arange(n, dtype=np.int64)

    r = rng.random(n)
    side_draw = rng.integers(0, 2, n).astype(np.uint8)
    lvl = rng.integers(0, p.n_levels, n)
    size = (rng.integers(1, 6, n) * 100).astype(np.int64)

    live: List[int] = []
    nxt = 1
    mid = p.init_mid
    for i in range(n):
        sides[i] = side_draw[i]
        qtys[i] = size[i]
        if r[i] < 0.55 or not live:
            types[i] = 1
            ids[i] = nxt
            prices[i] = (mid - 1 - lvl[i]) if side_draw[i] == 0 else (mid + 1 + lvl[i])
            live.append(nxt)
            nxt += 1
        elif r[i] < 0.90:
            types[i] = 2
            j = int(r[i] * 1e6) % len(live)
            ids[i] = live[j]
            live[j] = live[-1]
            live.pop()
        else:
            types[i] = 4
            ids[i] = nxt
            nxt += 1
            qtys[i] = max(10, size[i] // 2)
    return types, ids, sides, prices, qtys, ts
