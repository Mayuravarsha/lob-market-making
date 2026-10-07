"""Market-making strategies.

WHAT A MARKET MAKER ACTUALLY DOES
---------------------------------
It posts a bid and an ask at the same time and hopes both get hit. Buy at
$10.00, sell at $10.02, keep $0.02. Do that ten thousand times a day on small
size and the pennies add up. You are being paid for providing immediacy: other
people want to trade RIGHT NOW and you are the one willing to be on the other
side.

THE TWO THINGS THAT KILL YOU
----------------------------
1. INVENTORY RISK. Suppose only your bid gets hit, ten times in a row. Now you
   are long 10,000 shares and you never wanted a directional position. If the
   price drops you lose far more than all the spread you earned.

   The fix is SKEW. When you are long, move BOTH quotes down. Your bid becomes
   less attractive (you buy less) and your ask becomes more attractive (you
   sell more). Inventory gets pulled back toward zero automatically. That is
   the single most important idea in market making and it is one line of code:

       reservation_price = reference_price - skew * inventory

2. ADVERSE SELECTION. The people trading against you sometimes know something.
   If your offer gets lifted right before the price jumps, you sold too cheap.
   You cannot avoid this entirely -- you can only charge for it, by quoting a
   wide enough spread, and detect it, by measuring markouts (see metrics.py).

TWO STRATEGIES ARE IMPLEMENTED
------------------------------
* InventorySkewMM   -- the practical version. Fixed half-spread, linear skew.
                       Every parameter has an obvious meaning. Start here.
* AvellanedaStoikovMM -- the textbook version. Solves a stochastic control
                       problem for the optimal quotes; the skew and the spread
                       both fall out of the maths instead of being chosen.
                       Being able to explain this paper is a real interview
                       differentiator.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

BUY, SELL = 0, 1


def _apply_price_band(bid_px: int, ask_px: int, ref: float,
                      max_dist: float):
    """Refuse to quote further than `max_dist` ticks from the reference price.

    Clamping rather than rejecting keeps the strategy quoting during the
    episode instead of going dark; the band is wide enough that it never binds
    in normal conditions and only ever catches a runaway.
    """
    lo = int(math.floor(ref - max_dist))
    hi = int(math.ceil(ref + max_dist))
    bid_px = max(lo, min(bid_px, hi))
    ask_px = max(lo, min(ask_px, hi))
    if ask_px <= bid_px:
        ask_px = bid_px + 1
    return bid_px, ask_px


@dataclass
class Quote:
    """What the strategy wants resting in the market right now.

    Size 0 means "do not quote that side" -- used when inventory is at its cap.
    """
    bid_price: Optional[int] = None
    bid_size: int = 0
    ask_price: Optional[int] = None
    ask_size: int = 0


@dataclass
class StrategyParams:
    half_spread_ticks: float = 1.0
    # Ticks of skew per `quote_size` shares of inventory. With quote_size=100
    # and skew=0.6, being long 500 shares moves both quotes down 3 ticks.
    skew_ticks: float = 0.6
    quote_size: int = 50
    max_inventory: int = 500
    # Use the microprice (size-weighted) instead of the mid as the anchor.
    # Almost always better: it already accounts for queue imbalance.
    use_microprice: bool = True
    # Do not churn the quote for a sub-tick change -- every cancel/replace
    # throws away queue position, which is the most valuable thing you own.
    requote_tol_ticks: float = 0.5
    # Minimum seconds between quote updates. Stands in for real-world latency
    # and for exchange message-rate limits.
    min_requote_interval_s: float = 0.05
    # Taper size down as inventory approaches the cap instead of cutting to 0.
    taper: bool = True

    # PRE-TRADE PRICE BAND. No quote may be further than this many ticks from
    # the reference price. This is not a tuning knob, it is a SAFETY LIMIT, and
    # it exists because of a real bug found while building this project:
    #
    #   the Avellaneda-Stoikov volatility estimate spiked -> the reservation
    #   price ran away -> the strategy posted a bid at -402,950 -> that became
    #   the best bid -> the flow simulator, which anchors its sell orders to the
    #   best bid, dumped sell orders far below the market and wiped out the
    #   entire bid side -> the mid went negative -> the vol estimate spiked
    #   further. A closed feedback loop from one unbounded number.
    #
    # Every real trading system has this check (exchanges enforce their own
    # price bands and limit-up/limit-down halts for the same reason). An
    # unbounded price is never a valid order, however confident the model is.
    max_quote_distance_ticks: float = 50.0
    # Hard ceiling on the estimated volatility, in ticks per sqrt(second).
    # Stops one bad print from poisoning the EWMA.
    vol_cap_ticks: float = 3.0

    # --- Avellaneda-Stoikov only --------------------------------------------
    gamma: float = 0.06        # risk aversion; higher = flatter inventory
    k_arrival: float = 1.4     # fill-intensity decay with distance from mid
    horizon_s: float = 30.0    # planning horizon used in place of (T - t)
    vol_halflife_s: float = 120.0  # EWMA half-life for the mid-price vol estimate
    # MICROSTRUCTURE NOISE. Do NOT estimate volatility from every event.
    #
    # The observed mid is the efficient price plus noise: bid-ask bounce, one
    # order arriving at the touch and flipping the microprice, a level emptying
    # and refilling. That noise does not accumulate over time but the squared
    # returns you compute from it DO get divided by an ever smaller dt, so the
    # realised-volatility estimate blows up as you sample faster. This is the
    # classic high-frequency "volatility signature plot" effect, and it is why
    # practitioners sample realised variance at 5-30 seconds rather than at
    # every tick.
    #
    # Concretely in this project: sampling every event gave sigma ~ 5 ticks per
    # sqrt(second) when the true value was 0.6 -- an 8x overestimate -- which
    # made Avellaneda-Stoikov quote 50 ticks wide and stop market making
    # altogether. Sampling once a second fixes it.
    vol_sample_interval_s: float = 5.0


# ---------------------------------------------------------------------------
class InventorySkewMM:
    """Fixed half-spread with linear inventory skew."""

    name = "inventory_skew"

    def __init__(self, params: Optional[StrategyParams] = None) -> None:
        self.p = params or StrategyParams()

    def reference(self, book) -> Optional[float]:
        if self.p.use_microprice:
            mp = book.microprice
            if mp is not None:
                return float(mp)
        m = book.mid
        return None if m is None else float(m)

    def quote(self, book, inventory: int, t: float) -> Optional[Quote]:
        p = self.p
        ref = self.reference(book)
        if ref is None:
            return None
        best_bid, best_ask = book.best_bid, book.best_ask
        if best_bid is None or best_ask is None:
            return None

        # --- the skew ---------------------------------------------------
        # Long inventory -> reservation price BELOW the reference -> we bid
        # lower (buy less) and offer lower (sell more). Short -> the reverse.
        inv_units = inventory / max(1, p.quote_size)
        reservation = ref - p.skew_ticks * inv_units

        bid_px = int(math.floor(reservation - p.half_spread_ticks))
        ask_px = int(math.ceil(reservation + p.half_spread_ticks))

        # Stay passive: never quote through the opposite touch, or we would be
        # paying the spread instead of earning it.
        bid_px = min(bid_px, best_ask - 1)
        ask_px = max(ask_px, best_bid + 1)
        if bid_px >= ask_px:                       # keep our own quotes uncrossed
            ask_px = bid_px + 1
        bid_px, ask_px = _apply_price_band(bid_px, ask_px, ref,
                                           p.max_quote_distance_ticks)

        # --- position limits --------------------------------------------
        bid_sz, ask_sz = p.quote_size, p.quote_size
        if p.taper:
            room_long = max(0.0, (p.max_inventory - inventory) / p.max_inventory)
            room_short = max(0.0, (p.max_inventory + inventory) / p.max_inventory)
            bid_sz = int(p.quote_size * min(1.0, room_long))
            ask_sz = int(p.quote_size * min(1.0, room_short))
        else:
            if inventory >= p.max_inventory:
                bid_sz = 0
            if inventory <= -p.max_inventory:
                ask_sz = 0

        return Quote(bid_px, max(0, bid_sz), ask_px, max(0, ask_sz))


# ---------------------------------------------------------------------------
class AvellanedaStoikovMM:
    """Avellaneda & Stoikov (2008), 'High-frequency trading in a limit order book'.

    THE PROBLEM THEY SOLVE
    ----------------------
    You hold inventory q. The mid price S follows a random walk with volatility
    sigma. Your orders get filled with a probability that decays exponentially
    the further you quote from the mid: P(fill) ~ A * exp(-k * distance).
    You have exponential utility with risk aversion gamma. Where do you quote?

    THE ANSWER (two formulas worth memorising)
    ------------------------------------------
        reservation price   r = S - q * gamma * sigma^2 * (T - t)
        optimal total spread  = gamma * sigma^2 * (T - t) + (2/gamma) * ln(1 + gamma/k)

    Read them like this:

    * The reservation price is your PERSONAL fair value. It is the mid adjusted
      for the fact that you already hold q. Long inventory pushes it down: you
      genuinely value the asset less than the market does, because taking more
      of it adds risk you are already carrying. Note that the skew scales with
      sigma^2 -- in a volatile market you de-risk far more aggressively. The
      fixed-skew strategy above cannot do that.

    * The spread has two pieces. `gamma*sigma^2*(T-t)` is compensation for
      inventory risk. `(2/gamma)*ln(1+gamma/k)` is the monopoly-pricing term:
      quote wider and earn more per fill but get fewer fills, and this is the
      optimum of that trade-off. Notice it does not depend on q at all --
      inventory changes WHERE you centre your quotes, not HOW WIDE they are.

    PRACTICAL ADAPTATIONS MADE HERE
    -------------------------------
    * (T - t) is replaced by a fixed `horizon_s`. The original model has a hard
      end-of-day at T where the trader is forced flat, which makes the skew blow
      up in the final seconds. Real desks run the steady-state version.
    * sigma is estimated online with an EWMA of mid-price returns rather than
      assumed known.
    """

    name = "avellaneda_stoikov"

    def __init__(self, params: Optional[StrategyParams] = None) -> None:
        self.p = params or StrategyParams()
        self._last_mid: Optional[float] = None
        self._last_t: Optional[float] = None
        self._var: float = 0.01          # EWMA variance of mid moves, ticks^2/s
        self.sigma: float = 0.1

    def _update_vol(self, mid: float, t: float) -> None:
        # Sample on a fixed time grid, not on every event -- see the note on
        # vol_sample_interval_s. Returning early here is the whole fix.
        if self._last_t is not None and (t - self._last_t) < self.p.vol_sample_interval_s:
            return
        if self._last_mid is not None and self._last_t is not None:
            dt = max(1e-6, t - self._last_t)
            inst = (mid - self._last_mid) ** 2 / dt   # per-second variance
            lam = 0.5 ** (dt / max(1e-6, self.p.vol_halflife_s))
            self._var = lam * self._var + (1.0 - lam) * inst
        self._last_mid, self._last_t = mid, t
        # Cap the estimate. An EWMA fed one absurd print will otherwise carry
        # that print for many half-lives, and every quote in between is wrong.
        self.sigma = min(self.p.vol_cap_ticks, math.sqrt(max(self._var, 1e-8)))
        self._var = min(self._var, self.p.vol_cap_ticks ** 2)

    def quote(self, book, inventory: int, t: float) -> Optional[Quote]:
        p = self.p
        mid = book.mid
        if mid is None:
            return None
        best_bid, best_ask = book.best_bid, book.best_ask
        if best_bid is None or best_ask is None:
            return None

        self._update_vol(float(mid), t)
        ref = float(book.microprice) if (p.use_microprice and book.microprice is not None) else float(mid)

        q_units = inventory / max(1, p.quote_size)
        var_term = p.gamma * (self.sigma ** 2) * p.horizon_s

        reservation = ref - q_units * var_term
        total_spread = var_term + (2.0 / p.gamma) * math.log(1.0 + p.gamma / p.k_arrival)
        half = max(0.5, 0.5 * total_spread)

        bid_px = int(math.floor(reservation - half))
        ask_px = int(math.ceil(reservation + half))
        bid_px = min(bid_px, best_ask - 1)
        ask_px = max(ask_px, best_bid + 1)
        if bid_px >= ask_px:
            ask_px = bid_px + 1
        bid_px, ask_px = _apply_price_band(bid_px, ask_px, ref,
                                           p.max_quote_distance_ticks)

        bid_sz, ask_sz = p.quote_size, p.quote_size
        if inventory >= p.max_inventory:
            bid_sz = 0
        if inventory <= -p.max_inventory:
            ask_sz = 0
        return Quote(bid_px, bid_sz, ask_px, ask_sz)
