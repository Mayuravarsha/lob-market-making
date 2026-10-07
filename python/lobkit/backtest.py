"""The event-driven backtester.

HOW A HONEST MARKET-MAKING BACKTEST WORKS
-----------------------------------------
The naive version says "the price touched my quote, so I got filled". That is
wrong, and it is the single most common way people fool themselves into a
beautiful equity curve that evaporates in production.

Why it is wrong: when you post a bid at $10.00 there may already be 4,000
shares queued at $10.00 ahead of you. A 500-share seller comes in. Those 500
shares fill the FIRST 500 shares in the queue -- not yours. You only get filled
after 4,000 shares have traded at $10.00 while your order is still there. Very
often the price moves away first and you get nothing.

This backtester avoids the problem entirely by NOT modelling fills at all. Our
orders go into the same C++ book as everybody else's, and the matching engine
decides. If a market sell order sweeps down to our bid, the engine fills
whoever is at the head of the queue. If that is us, we get a trade. Queue
position is handled because it is real, not because we approximated it.

WHAT IS MEASURED
----------------
* mark-to-market equity, sampled on a fixed grid
* Sharpe ratio, maximum drawdown
* fill rate, spread capture, turnover
* MARKOUTS -- the honest measure of adverse selection. For every fill, where
  was the mid 1s / 5s / 30s later? If your buys are systematically followed by
  the price falling, you are being picked off, and no amount of Sharpe
  arithmetic will save you in production.

THE COST MODEL
--------------
US equity exchanges mostly run "maker-taker": you are PAID a rebate for
providing liquidity (a passive fill) and CHARGED a fee for taking it. Around
+$0.0020 and -$0.0030 per share respectively. A market maker who ignores the
rebate is understating revenue, sometimes by more than the whole spread edge on
a penny-wide name. It is a parameter here, and the report shows PnL both with
and without it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .simulate import FlowSimulator, SimParams
from .strategy import Quote, StrategyParams

BUY, SELL = 0, 1


@dataclass
class CostModel:
    tick_size: float = 0.01           # dollars per tick
    maker_fee_per_share: float = -0.0020   # NEGATIVE = we are paid a rebate
    taker_fee_per_share: float = 0.0030


@dataclass
class BacktestResult:
    # time series (numpy arrays, one entry per snapshot)
    t: np.ndarray
    mid: np.ndarray                  # in ticks
    inventory: np.ndarray            # shares
    cash: np.ndarray                 # dollars
    equity: np.ndarray               # dollars, mark-to-market
    spread: np.ndarray               # ticks

    # per-fill records
    fills: List[dict]

    # counters
    n_events: int = 0
    n_quotes_placed: int = 0
    n_quotes_cancelled: int = 0
    sim_seconds: float = 0.0
    backend: str = ""
    meta: Dict = field(default_factory=dict)


class Backtester:
    def __init__(self, strategy, sim_params: Optional[SimParams] = None,
                 costs: Optional[CostModel] = None,
                 snapshot_every: int = 25,
                 book_factory=None):
        from . import OrderBook, BACKEND  # local import to avoid a cycle

        self.strategy = strategy
        self.sp = sim_params or SimParams()
        self.costs = costs or CostModel()
        self.snapshot_every = snapshot_every
        self.backend = BACKEND

        self.book = (book_factory or OrderBook)(1 << 18)
        self.sim = FlowSimulator(self.book, self.sp)

        # Our own orders: id -> {side, price, qty}
        self.my: Dict[int, dict] = {}
        self.my_bid_id: Optional[int] = None
        self.my_ask_id: Optional[int] = None
        self.next_my_id = 1                     # ours are low ids, sim uses 1e6+

        self.inventory = 0
        self.cash = 0.0
        self.fills: List[dict] = []
        self.n_placed = 0
        self.n_cancelled = 0
        self.queue_ahead_samples: List[int] = []
        self.market_volume = 0        # every share traded by anyone
        self.n_risk_rejects = 0
        self.max_order_distance_ticks = 100.0
        # Hard cap, deliberately looser than the strategy's own max_inventory:
        # the strategy should manage inventory, this only catches a failure.
        self.hard_position_limit = int(3 * getattr(strategy.p, "max_inventory", 500))
        self._last_quote_t = -1e18

    # ------------------------------------------------------------------ fills
    def _apply_fill(self, side: int, price_ticks: int, qty: int, t: float,
                    mid_ticks: float, passive: bool, queue_ahead: int = -1) -> None:
        c = self.costs
        px = price_ticks * c.tick_size
        fee = (c.maker_fee_per_share if passive else c.taker_fee_per_share) * qty

        if side == BUY:
            self.inventory += qty
            self.cash -= px * qty
        else:
            self.inventory -= qty
            self.cash += px * qty
        self.cash -= fee                       # a rebate is a negative fee

        self.fills.append({
            "t": t,
            "side": side,
            "price": price_ticks,
            "qty": qty,
            "mid_at_fill": mid_ticks,
            "passive": passive,
            "fee": fee,
            "queue_ahead_at_entry": queue_ahead,
            "inventory_after": self.inventory,
        })

    def _process_trades(self, new_trades, t: float, mid_ticks: float) -> None:
        """Look for our order ids on the resting side of new trades."""
        if not new_trades:
            return
        for tr in new_trades:
            rid = tr.resting_id
            o = self.my.get(rid)
            if o is None:
                continue
            qty = int(tr.qty)
            self._apply_fill(o["side"], int(tr.price), qty, t, mid_ticks,
                             passive=True, queue_ahead=o.get("queue_ahead", -1))
            o["qty"] -= qty
            if o["qty"] <= 0:
                self.my.pop(rid, None)
                if self.my_bid_id == rid:
                    self.my_bid_id = None
                if self.my_ask_id == rid:
                    self.my_ask_id = None

    # ----------------------------------------------------------------- quotes
    def _cancel(self, oid: Optional[int]) -> None:
        if oid is None:
            return
        if self.book.cancel(oid):
            self.n_cancelled += 1
        self.my.pop(oid, None)

    def _place(self, side: int, price: int, size: int, ts_ns: int) -> Optional[int]:
        if size <= 0:
            return None

        # PRE-TRADE RISK CHECK. Independent of the strategy's own band: the
        # order-management layer must never trust the model. Two limits, both
        # standard on a real desk:
        #   1. price band  -- reject any quote absurdly far from the market
        #   2. position    -- reject anything that would breach the hard cap
        ref = self.book.mid
        if ref is not None and abs(price - ref) > self.max_order_distance_ticks:
            self.n_risk_rejects += 1
            return None
        projected = self.inventory + (size if side == BUY else -size)
        if abs(projected) > self.hard_position_limit:
            self.n_risk_rejects += 1
            return None
        oid = self.next_my_id
        self.next_my_id += 1
        rep = self.book.add_limit(oid, side, int(price), int(size), ts_ns)
        self.n_placed += 1
        resting = int(rep.resting_qty)
        if resting > 0:
            # Queue position at the moment we joined. This is the single best
            # predictor of whether the order will ever fill, and it is only
            # available because our order really is in the book.
            qa = self.book.queue_ahead(oid)
            self.my[oid] = {"side": side, "price": int(price), "qty": resting,
                            "queue_ahead": int(qa) if qa is not None else -1}
            self.queue_ahead_samples.append(int(qa) if qa is not None and qa >= 0 else 0)
            return oid
        return None

    def _sync_quotes(self, q: Quote, t: float, ts_ns: int) -> None:
        """Move our resting quotes toward the strategy's desired quote.

        The key subtlety: if the desired price is unchanged we leave the order
        alone. Cancel/replace would send it to the BACK of the queue, throwing
        away the one asset a market maker accumulates for free -- time priority.
        """
        tol = self.strategy.p.requote_tol_ticks

        # --- bid ---
        cur = self.my.get(self.my_bid_id) if self.my_bid_id is not None else None
        want_px, want_sz = q.bid_price, q.bid_size
        if want_sz <= 0:
            if cur is not None:
                self._cancel(self.my_bid_id)
                self.my_bid_id = None
        elif cur is None:
            self.my_bid_id = self._place(BUY, want_px, want_sz, ts_ns)
        elif abs(cur["price"] - want_px) > tol:
            self._cancel(self.my_bid_id)
            self.my_bid_id = self._place(BUY, want_px, want_sz, ts_ns)

        # --- ask ---
        cur = self.my.get(self.my_ask_id) if self.my_ask_id is not None else None
        want_px, want_sz = q.ask_price, q.ask_size
        if want_sz <= 0:
            if cur is not None:
                self._cancel(self.my_ask_id)
                self.my_ask_id = None
        elif cur is None:
            self.my_ask_id = self._place(SELL, want_px, want_sz, ts_ns)
        elif abs(cur["price"] - want_px) > tol:
            self._cancel(self.my_ask_id)
            self.my_ask_id = self._place(SELL, want_px, want_sz, ts_ns)

    # ------------------------------------------------------------------- loop
    def run(self, n_events: int = 200_000, progress_every: int = 0) -> BacktestResult:
        c = self.costs
        ts_list, mid_list, inv_list, cash_list, eq_list, spr_list = [], [], [], [], [], []

        prev_mid = self.book.mid

        for i in range(n_events):
            # The mid BEFORE this event is the correct reference price for
            # measuring how much edge a fill captured. Using the mid AFTER the
            # event double-counts the event's own price impact: a market sell
            # that hits our bid also pushes the mid down, so a post-trade
            # comparison makes every passive buy look like it happened above
            # the mid. That single mistake flips spread capture negative and
            # makes a profitable strategy look broken.
            mid_before = prev_mid

            ev = self.sim.step()
            if ev is None:
                break
            t = self.sim.t
            new_trades = self.sim.sync_fills()

            mid = self.book.mid
            mid_ticks = float(mid) if mid is not None else float("nan")
            prev_mid = mid

            ref_mid = float(mid_before) if mid_before is not None else mid_ticks
            for tr in new_trades:
                self.market_volume += int(tr.qty)
            self._process_trades(new_trades, t, ref_mid)

            # Keep the shared trade buffer from growing without bound.
            if self.book.n_trades_buffered() > 200_000:
                self.book.clear_trades()
                self.sim.reset_trade_cursor()

            # Re-quote, respecting the minimum interval (our "latency").
            if t - self._last_quote_t >= self.strategy.p.min_requote_interval_s:
                q = self.strategy.quote(self.book, self.inventory, t)
                if q is not None:
                    self._sync_quotes(q, t, int(t * 1e9))
                self._last_quote_t = t

            if i % self.snapshot_every == 0 and mid is not None:
                sp = self.book.spread
                equity = self.cash + self.inventory * mid_ticks * c.tick_size
                ts_list.append(t)
                mid_list.append(mid_ticks)
                inv_list.append(self.inventory)
                cash_list.append(self.cash)
                eq_list.append(equity)
                spr_list.append(sp if sp is not None else np.nan)

            if progress_every and i and i % progress_every == 0:
                print(f"    {i:>9,} events  t={t:8.1f}s  inv={self.inventory:>6}  "
                      f"equity=${self.cash + self.inventory*mid_ticks*c.tick_size:10.2f}  "
                      f"fills={len(self.fills)}")

        return BacktestResult(
            t=np.asarray(ts_list),
            mid=np.asarray(mid_list),
            inventory=np.asarray(inv_list, dtype=float),
            cash=np.asarray(cash_list),
            equity=np.asarray(eq_list),
            spread=np.asarray(spr_list, dtype=float),
            fills=self.fills,
            n_events=self.sim.n_events,
            n_quotes_placed=self.n_placed,
            n_quotes_cancelled=self.n_cancelled,
            sim_seconds=self.sim.t,
            backend=self.backend,
            meta={
                "market_volume": self.market_volume,
                "n_risk_rejects": self.n_risk_rejects,
                "mean_queue_ahead": (float(np.mean(self.queue_ahead_samples))
                                     if self.queue_ahead_samples else float("nan")),
                "n_jumps": getattr(self.sim, "n_jumps", 0),
                "strategy": getattr(self.strategy, "name", "unknown"),
                "tick_size": c.tick_size,
                "maker_fee_per_share": c.maker_fee_per_share,
                "final_inventory": self.inventory,
            },
        )
