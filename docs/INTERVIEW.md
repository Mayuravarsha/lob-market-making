# Interview preparation: the limit order book project

Read `CONCEPTS.md` first. This file is the compressed, question-and-answer form.

Two rules before anything else:

1. **Never claim more than you did.** "It's a simulated market, and here are the
   three biggest reasons the Sharpe is not real" beats a confident wrong answer
   every single time. Interviewers are testing whether you know the limits of
   your own work.
2. **Lead with the interesting failure.** Every candidate's project "works". Very
   few can describe a subtle bug they found and what it taught them. That is the
   part they remember.

---

## The 60-second version

> I built a price-time-priority limit order book in C++17 — intrusive FIFO
> queues per price level, a pool allocator, and a hash map from order id to slot
> so cancel is O(1), which matters because over 90% of real orders are cancelled
> rather than filled. It does about 6.25 million mixed events per second with a
> 37 nanosecond median add and a 107 nanosecond p99 once the pages are
> pre-faulted.
>
> I exposed it to Python with pybind11 and built a market-making backtester on
> top. The strategy quotes around the microprice with inventory-based skew.
> Crucially I don't model fills — my orders go into the same book as the
> simulated crowd and the matching engine decides, so queue position is real
> rather than approximated.
>
> The result I care about isn't the PnL, it's the attribution: I capture 1.4
> cents a share of spread, hand back 1.05 cents to adverse selection, and the
> maker rebate is another 0.2 — so about 0.55 cents net, at 9% participation.
> That decomposition is the thing that told me the backtest was measuring
> something real.

Then stop, and let them pick a thread.

---

## Order book and systems

**Q: Walk me through your data structure.**

Three parts. A `std::map` from price to price-level, sorted so `begin()` is
always the best price — O(log L) to find a level, O(1) to read the top of book.
Each level is an intrusive doubly-linked list of orders in arrival order, giving
FIFO time priority, with `total_qty` cached so depth queries are O(1). And an
`unordered_map` from order id to a slot index, which is what makes cancel O(1) —
without it you'd have to search every level.

**Q: Why is cancel the operation you optimised for?**

Because in US equities well over 90% of orders are cancelled, not filled. Adds
and cancels are the hot path; matching is comparatively rare. Anyone who
optimises `add` and forgets `cancel` has misunderstood the workload.

**Q: Why a map and not a flat array indexed by tick?**

A flat array gives O(1) price lookup instead of O(log L), and real HFT engines
often use one. The costs are memory proportional to the price range and re-basing
when the price drifts outside your window. I chose the map because L is small —
tens of levels — so log L is 5 or 6 comparisons, and the code stays readable. If
I needed another 2× I'd switch, and I'd expect most of the win to come from cache
locality rather than the complexity class.

**Q: Why indices instead of pointers?**

The pool is a `std::vector`, so growing it invalidates every pointer into it.
Indices survive reallocation. They're also half the size — `uint32_t` covers 4
billion live orders — which keeps the `Order` struct small and fits more orders
per cache line.

**Q: What does your pool allocator buy you?**

Allocation becomes "pop the head of a free list" — a few instructions, no lock,
and the memory stays hot in cache. The alternative is `new`/`delete` per order,
which takes a lock and scatters allocations across RAM. In a loop doing millions
of add/cancel pairs per second, the allocator would be most of the runtime.

**Q: Your p99 was 13× worse than your median at one point. Why?**

Soft page faults. `reserve()` only reserves virtual addresses; physical pages are
attached lazily on first write, and each first touch traps into the kernel for
1–2 microseconds. Pre-faulting — resizing the vector to its capacity once at
startup so every page gets written — took the add p99 from 1,386 ns to 107 ns
with no change to the median. In production you'd pair that with `mlock()` and
huge pages.

**Q: Why do you report percentiles instead of the mean?**

Because in trading the mean is nearly useless. An engine averaging 30 ns that
stalls 40 µs once every 100,000 orders will lose money exactly during bursts —
and bursts are when the slow path triggers and when the money is. I also measure
the clock's own overhead, about 20 ns, and subtract it, because timing a 30 ns
operation with a 20 ns clock is not a rounding error.

**Q: What's the difference between a market order, an IOC and a FOK?**

A market order has no price limit and sweeps until filled or the book is empty.
An IOC is a limit order that fills what it can immediately and discards the
remainder rather than resting. A FOK is all-or-nothing: it fills completely and
immediately or does nothing at all. FOK has to check available liquidity *before*
touching the book, otherwise you'd have to unwind partial fills.

**Q: If I shrink a resting order, do I keep my place in the queue? If I grow it?**

Shrink at the same price: you keep it. Grow, or change price: you lose it and go
to the back. The asymmetry exists because growing would otherwise let you jump
the people queued behind you — everyone would post 1 share for a good queue spot
and grow it when they wanted to trade.

**Q: How do you know your matching engine is correct?**

Three layers. 81 unit tests covering every rule. A 200,000-operation invariant
fuzz — the book must never be crossed, cached level sizes must equal the sum of
their orders, no empty levels left behind. And differential testing: I wrote a
completely independent pure-Python implementation with a different data structure
and fed both 120,000 adversarial events, comparing every observable after every
event. Zero mismatches. That catches the bug you never thought to write a test
for, which is how exchanges and compilers get validated.

---

## Market making

**Q: How does a market maker make money?**

By posting a bid and an ask at the same time and earning the spread when both get
hit. You're being paid for providing immediacy — other people want to trade right
now and you're willing to be on the other side. On many venues you're also paid a
maker rebate, which in my backtest was 36% of total PnL.

**Q: What's your biggest risk?**

Two. Inventory risk — if only one side keeps getting hit you accumulate a
directional position you never wanted, and one adverse move can wipe out weeks of
spread. And adverse selection, which is the bigger one.

**Q: Explain adverse selection.**

The people trading against you sometimes know something. If your offer gets
lifted right before the price jumps, you sold too cheap — and they chose to trade
with you *precisely because* your quote was stale. Structurally, you're
systematically on the wrong side of every trade you do. That's not a flaw in the
strategy, it's the definition of providing liquidity. You can't avoid it; you can
only charge enough spread to cover it and measure it so you know whether you are.

**Q: How do you measure it?**

Markouts. For each fill, look at the mid `h` seconds later, signed by trade
direction. But you have to decompose, because a positive markout doesn't mean
zero adverse selection:

```
markout = spread capture + post-trade drift
```

Spread capture is the edge you quoted — positive by construction for a passive
fill. Post-trade drift is the actual adverse selection. Mine ran +1.40 cents
captured, −1.05 cents of drift, +0.20 of rebate, so about +0.55 net per share.

**Q: What does the skew term do?**

`reservation = reference − skew × inventory`. When you're long, both quotes move
down: your bid gets less attractive so you buy less, your ask gets more
attractive so you sell more. Inventory mean-reverts to zero automatically.
Turning it off in my ablations took inventory standard deviation from 51 shares
to 315 and nearly doubled drawdown.

**Q: PnL went *up* when you turned skew off. So why keep it?**

Because in that particular simulated market, holding inventory happened to pay.
Skew costs a little expected return to buy a large reduction in risk. If you
judge risk management by PnL alone you'll always conclude it's a waste — right up
until the day it isn't. The relevant comparison is return per unit of risk and
the tail, not the mean.

**Q: Why anchor on the microprice instead of the mid?**

The microprice weights each side's price by the *opposite* side's size. If
there's 5,000 bid and 100 offered, the ask gets consumed first, so fair value
sits closer to the ask. Empirically the microprice is a better short-horizon
predictor of the future mid than the mid itself, which means the mid isn't a
martingale at high frequency and there's information in the queue sizes.

**Q: Explain Avellaneda–Stoikov.**

Two formulas. The reservation price `r = S − q·γ·σ²·(T−t)` is your personal fair
value given inventory `q` — long inventory shades it down because more of the
asset adds to risk you already carry. The optimal spread is
`γ·σ²·(T−t) + (2/γ)·ln(1 + γ/k)`: the first term is compensation for inventory
risk, the second is monopoly pricing — quote wider, earn more per fill, get fewer
fills, and that's the optimum.

Two things worth noticing. The skew scales with σ², so you de-risk automatically
in a volatile market, which a fixed-skew rule can't. And the spread doesn't
depend on `q` at all — inventory changes where you centre your quotes, not how
wide they are.

**Q: What did you change from the paper?**

`(T−t)` becomes a fixed horizon, because the original has a hard end-of-day where
the trader is forced flat and the skew blows up in the last seconds; desks run
the steady-state version. And σ is estimated online rather than assumed known —
which turned out to be where all the difficulty was.

---

## Backtesting and statistics

**Q: How do you model fills?**

I don't. That's the point. My orders go into the same C++ book as the simulated
crowd and the matching engine decides. If a market sell sweeps to my bid, whoever
is at the head of that queue gets filled. Queue position is correct because it's
real. My orders joined with a mean of 157 shares ahead of them.

**Q: Why does that matter?**

The naive backtest says "the price touched my quote so I got filled". If there
were 4,000 shares queued ahead of you, a 500-share seller fills them, not you.
You only trade after 4,000 shares have gone through at that price while you're
still there — and very often the price moves away first. Assuming touch-equals-
fill is the single most common way people produce an equity curve that evaporates
in production.

**Q: Your Sharpe is over 1,500. Do you believe it?**

No, and the code says so in its own output. Three reasons. The simulated crowd
doesn't react to me — I'm taking 9% of all volume with no competitive response,
and a real market would have someone undercutting me within milliseconds.
Annualising per-second PnL assumes i.i.d. returns, and high-frequency PnL is
autocorrelated, which biases it upward. And there's one instrument, one venue, no
outages, no latency jitter, no regime changes.

I also compute a cross-run Sharpe — ten independent simulations, each treated as
one observation — which makes no i.i.d. assumption. That's ~2,165, still far too
high for the structural reasons above. The honest summary is that this measures
whether the strategy logic is coherent, not what it would earn.

**Q: What's a realistic Sharpe for a real market-making strategy?**

Firm-level figures in the high single digits to low tens are commonly cited, and
individual strategies can be higher intraday. Anything in the hundreds is a
simulation artefact. The gap between my number and reality is almost entirely
competition and market impact.

**Q: How would you make it realistic?**

In order of impact: add competing market makers who react to my quotes; make
market impact endogenous so my participation changes the flow; make latency a
random variable with a fat tail; add intraday seasonality and auctions; then run
it on real LOBSTER data, which I've already written the loader and a
reconstruction validator for.

**Q: Talk me through your ablations.**

Remove one ingredient at a time and predict the result before running it. No
skew: inventory std dev 51 → 315, drawdown +79%. No rebate: PnL −36%. Wide quotes
(3-tick half-spread): half the fills, more than double the edge each, higher
Sharpe. Slow requoting at 500 ms: 58% fewer fills.

The interesting one was turning off informed flow — PnL *fell* from $1,670 to
$308 and drawdown grew 5×, the opposite of what I predicted. Informed flow makes
the mid track the hidden fair value, which makes it partially predictable, and my
microprice-anchored quotes were exploiting that. I didn't anticipate it; it's in
the results anyway.

**Q: What's the volatility signature plot in your results?**

An accident that turned into the best chart in the project. My A–S strategy was
quoting 50 ticks wide because its volatility estimate was 8× too high. The cause
is microstructure noise: the observed mid is the efficient price plus bid-ask
bounce, that noise doesn't accumulate over time, but squared returns get divided
by an ever-smaller `dt` as you sample faster — so realised volatility *diverges*
as sampling speeds up. Sampling every event gave 5.7 ticks/√s against a true 0.6;
at 60-second sampling it converges to 0.54. That's why practitioners compute
realised variance at 5–30 seconds, not tick by tick.

---

## The bug stories (have two of these ready)

**The benchmark that measured nothing.** `BM_Cancel` reported 7.5 ns. Google
Benchmark picks iteration counts at runtime — it asked for 24 million and I'd
pre-built 2 million orders, so most iterations cancelled nothing, and a failed
cancel is one hash lookup. I was benchmarking the speed of doing nothing. Fix:
chunked refills, plus a counter that must equal the iteration count. A benchmark
needs its own assertion.

**The negative spread capture.** The backtest showed the market maker buying
*above* the mid on average — structurally impossible for a passive strategy. I
was recording the mid *after* the event, but the event that fills your bid is a
market sell, which also pushes the mid down. The measurement was double-counting
the trade's own impact. Fix: use the pre-event mid. Lesson: when a metric has an
impossible sign, question the measurement, not the strategy.

**The bid at −402,950.** The best one. A–S's volatility estimate spiked, the
reservation price ran away, and the strategy posted a hugely negative bid — my
clamp only capped quotes from above. That bid became the best bid; the flow
simulator anchors its sells to the best bid, so it dumped sells far below the
market and wiped out the entire bid side; the mid went negative; the vol estimate
spiked further. One unbounded number destroyed the whole market through a
feedback path I hadn't imagined. Fix: a price band in the strategy *and* an
independent pre-trade risk check in the OMS that never trusts the model.
Exchanges have price bands and limit-up/limit-down halts for exactly this reason.

**The O(n²) that looked like a getter.** `trades()` returned the trade vector,
and pybind11's `std::vector` caster copies — so calling it once per event
rebuilt a Python list of every trade so far. Adding `trades_since(cursor)` made
the backtest 35× faster. Neither this nor the pre-faulting bug produced a wrong
answer; both were invisible without measuring.

---

## Questions to ask them

- How do you handle queue position in your own backtests — model it, or replay
  through a matching engine?
- What markout horizons does the desk look at, and do you decompose spread
  capture from drift the way I did?
- Where does the firm sit on the flat-array-vs-tree question for the book, and
  did that come from profiling or from convention?
- How much of the alpha in market making is the quoting model versus pure speed,
  in your experience?

---

## Things not to say

- "It's profitable" as the headline. Lead with the attribution.
- Quoting the Sharpe without immediately caveating it.
- "I used real market data" if you ran the simulator. Say which, precisely.
- Claiming the C++ is "production grade". It's clean, tested and fast; it has no
  persistence, no recovery, no FIX gateway and no multithreading.
- Pretending the ablation that surprised you confirmed your prediction.
