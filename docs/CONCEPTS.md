# Everything in this project, explained from zero

You do not need to know anything about trading to read this. It builds up from
"what is a stock exchange" to "why my market maker loses 1 cent per share to
adverse selection and makes it back on spread and rebates".

Read it once end to end. Then read the code — every file has a long header
comment that assumes you have read this.

---

## Table of contents

1. [What actually happens when you buy a stock](#1)
2. [The limit order book](#2)
3. [Price-time priority: the matching rule](#3)
4. [The data structure, and why each piece is what it is](#4)
5. [Making it fast: pools, cache, and page faults](#5)
6. [How you know the engine is correct](#6)
7. [Reading a Level-2 book: mid, microprice, imbalance](#7)
8. [What a market maker does](#8)
9. [The two things that kill market makers](#9)
10. [Avellaneda–Stoikov, derived in words](#10)
11. [Simulating a market you can trust](#11)
12. [Backtesting without lying to yourself](#12)
13. [The metrics, and what each one is protecting you from](#13)
14. [Everything that is still wrong with this backtest](#14)
15. [The C++/Python boundary](#15)
16. [The bugs I hit, and what each one taught](#16)

---

<a name="1"></a>
## 1. What actually happens when you buy a stock

You press "buy" in an app. What happens next is not "the app finds you some
shares at the current price". There is no such thing as "the current price".
There is only a list of people willing to trade, and the prices they demand.

That list is the **order book**, and it lives on a computer at the exchange.
Everyone who wants to trade sends it a message. The exchange's job is to match
buyers to sellers according to a fixed, published rule. That is the entire
business.

Two kinds of order matter:

**A limit order** says: *"I will buy 100 shares, but I will pay no more than
$10.00."* If nobody will sell at $10.00 right now, your order **rests** on the
book and waits. You have added liquidity to the market — you are now one of the
people others can trade against. You might wait forever.

**A market order** says: *"Buy 100 shares. I do not care about the price, do it
now."* This one never waits. It immediately trades against whoever is resting on
the other side, taking the best prices available until it is filled. You have
removed liquidity.

That distinction — **passive** (resting, patient, providing) versus
**aggressive** (crossing, immediate, taking) — is the single most important
concept in market microstructure. Almost everything else follows from it:

- Passive orders get a **better price** but **might not trade at all**.
- Aggressive orders **always trade** but pay the **spread**.
- Exchanges usually **pay you** a rebate for passive fills and **charge you** a
  fee for aggressive ones, because they want resting orders on their book.

A market maker is someone whose entire business is being passive on both sides
at once.

---

<a name="2"></a>
## 2. The limit order book

Here is a book. This is what all the code in `include/lob/` is about.

```
        BIDS (people who want to buy)  |  ASKS (people who want to sell)
        price    size   orders         |  price    size   orders
        10.02     900      3           |  10.03    400      2    <- best ask
        10.01    1500      5           |  10.04   1100      4
        10.00    2200      9           |  10.05    700      3
          ^ best bid
```

- **Best bid** ($10.02) = the highest price anyone will pay.
- **Best ask** ($10.03) = the lowest price anyone will sell at.
- **Spread** = ask − bid = $0.01. This is the gap. Nothing trades until someone
  crosses it.
- **Mid price** = (10.02 + 10.03) / 2 = $10.025. This is a convenient fiction —
  nobody can actually trade there — but it is the standard reference point.
- **Depth** = the sizes at each level. This is "Level 2" data. "Level 1" is just
  the top of book.

If you send a market buy for 500 shares, you take 400 at $10.03 and then 100 at
$10.04. Your average price is $10.032. The extra $0.002 you paid above the touch
is **slippage**, and it is why large orders are expensive.

### Prices are integers

Notice that the code never stores $10.02 as a floating-point number. Exchanges
quote on a discrete grid — a **tick**, typically $0.01 for US stocks — so we
store $10.02 as the integer `1002`.

Two reasons, both serious:

1. **Floating point comparison is not exact.** `0.1 + 0.2 != 0.3` in IEEE 754.
   In a matching engine, "is this order's price better than that one's?" must be
   exactly right, every time, or you will fill the wrong person.
2. **Integer comparison is faster** than floating point, and this comparison
   happens millions of times a second.

Every real matching engine does this. See `include/lob/types.hpp`.

---

<a name="3"></a>
## 3. Price-time priority: the matching rule

When a sell order arrives, which of the resting buyers gets filled?

Almost every equity exchange in the world uses **price-time priority**:

1. **Price first.** The buyer offering the highest price wins. Obviously — they
   are offering the seller the best deal.
2. **Time second.** Among buyers offering the *same* price, whoever placed their
   order *first* gets filled first. First come, first served.

That is it. Two rules. Everything else in the engine is bookkeeping to make
those two rules fast.

### Why time priority matters so much

Suppose you post a bid at $10.00 and there are already 4,000 shares queued there
ahead of you. A seller comes in with 500 shares. Those 500 shares fill the *first*
500 shares in the queue. Not yours.

You only get filled after **4,000 shares have traded at $10.00 while your order
is still there**. If the price moves away before that happens, you get nothing.

Your position in that queue — **queue position** — is the single best predictor
of whether your order will ever trade. It is the most valuable asset a passive
trader accumulates, and it is free: you get it just by being early and by not
cancelling.

This is why the backtester in this project puts our orders in the *same book* as
everyone else's rather than modelling fills with a formula. Queue position is
handled correctly because it is *real*.

### The modify rule (a favourite interview question)

If you **reduce** the size of a resting order at the same price, you **keep**
your queue position.

If you **increase** the size, or change the price, you **lose** it and go to the
back of the queue.

Why the asymmetry? Because increasing your size would let you jump ahead of
people who queued behind you. If growing were free, everyone would post 1 share
to get a good queue spot and then grow it to 10,000 the moment they wanted to
trade. The rule closes that loophole.

Implemented in `OrderBook::modify` and tested in section 9 of
`tests/test_order_book.cpp`.

---

<a name="4"></a>
## 4. The data structure, and why each piece is what it is

The requirements:

| Operation | How often | Needs to be |
|---|---|---|
| add a limit order | ~55% of messages | fast |
| cancel an order | ~35% of messages | **very** fast |
| match an incoming aggressor | ~10% | fast |
| read the best bid/ask | after every message | free |

In real US equity markets, **over 90% of orders are cancelled, not filled**.
Cancel is the hot path. Anyone who optimises `add` and forgets `cancel` has
misunderstood the problem.

The structure has three parts:

### (a) Sorted prices → `std::map`

```cpp
std::map<Price, PriceLevel, std::greater<Price>> bids_;  // descending
std::map<Price, PriceLevel, std::less<Price>>    asks_;  // ascending
```

A `std::map` is a balanced binary tree: sorted, `O(log L)` lookup where L is the
number of distinct price levels. The crucial property is that `begin()` is
always the best price — so reading the top of book is `O(1)`, which is what we
need since a strategy reads it after every event.

*Could you do better?* Yes. Real HFT engines often use a **flat array indexed by
tick**, which makes price lookup `O(1)` instead of `O(log L)`. It costs memory
proportional to the price range and needs re-basing when the price drifts far.
For this project the map is the right trade: L is small (tens of levels), and
the code stays readable. Knowing the alternative — and why you did not use it —
is the interview answer.

### (b) FIFO queue at each price → intrusive doubly-linked list

Each `PriceLevel` is a linked list of orders in arrival order. **Intrusive**
means the `prev`/`next` links live *inside* the `Order` struct rather than in
separate node objects.

That is what makes cancel `O(1)`: given the order, you already have its
neighbours, so you can unlink it without searching.

Each level also caches `total_qty`, so "how much size is at $10.02?" is `O(1)`
rather than a walk. That cached number is what feeds the Level-2 display.

### (c) Find an order by id → `std::unordered_map<OrderId, Index>`

This is the piece that makes the whole thing work. Without it, cancelling order
12345 means searching every level — `O(N)` in the number of live orders. With
it, cancel is a hash lookup plus an `O(1)` unlink.

### Resulting complexity

| Operation | Cost | Measured on a 2.8 GHz cloud VM |
|---|---|---|
| add limit (no match) | `O(log L)` | **35 ns** |
| cancel | `O(1)` expected | **28 ns** |
| modify (shrink in place) | `O(1)` | **8.8 ns** |
| modify (reprice) | `O(log L)` | **73 ns** |
| match one resting order | `O(1)` | **48 ns** |
| best bid / microprice | `O(1)` | **2.1 ns** |
| top-10 depth snapshot | `O(n)` | **81 ns** |
| **mixed realistic flow** | — | **6.25 M events/sec** |

---

<a name="5"></ap>
## 5. Making it fast: pools, cache, and page faults

Three optimisations, each with a measurable effect.

### Pool allocation

The hot loop is add / cancel / add / cancel, millions of times per second. If
every add called `new` and every cancel called `delete`, you would spend most of
your time inside the system allocator, which takes a lock and hands back memory
scattered all over RAM.

Instead we allocate one big `std::vector<Order>` up front and hand out slots from
it. A freed slot goes on a singly-linked **free list** and gets reused by the
next add. Allocation becomes "pop the head of the free list" — three
instructions, no lock, and the memory stays hot in cache.

### Indices instead of pointers

The pool is a vector, so it can grow and invalidate every pointer into it.
Indices survive reallocation. They are also half the size of a 64-bit pointer
(`uint32_t` handles 4 billion live orders), which keeps the `Order` struct small
and therefore keeps more orders per cache line.

### Pre-faulting — the one that surprised me

`reserve()` does **not** give you memory. It reserves a range of *virtual*
addresses. The physical pages are attached lazily, the first time each 4 KiB page
is written, and each of those first touches is a **soft page fault**: a trap into
the kernel costing roughly 1–2 microseconds.

Spread across a run, that shows up as a fat tail. Measured on this project:

```
                        p50      p99    p99.9
add_limit, reserve only  38 ns  1386 ns  2411 ns
add_limit, pre-faulted   37 ns   107 ns  1754 ns
                                  ^^^^
                            13x better tail
```

The median is unchanged. The **p99 improves 13×**. `OrderPool::prefault()`
simply resizes the vector to its capacity once at startup, writing to every page
and moving all that cost to a moment when nobody cares.

This is standard practice in low-latency systems, usually alongside `mlock()` and
huge pages.

### Why the tail matters more than the mean

Google Benchmark reports the *mean*. In trading, the mean is nearly useless. An
engine that averages 30 ns but stalls for 40 µs once every 100,000 orders will
lose money precisely during bursts — because bursts are exactly when the slow
path triggers, and bursts are when the money is.

`bench/latency_profile.cpp` therefore times **every individual operation**,
sorts them, and reports p50 / p90 / p99 / p99.9 / max. It also measures the
clock's own overhead (~20 ns) and subtracts it, because when you are timing a
30 ns operation with a 20 ns clock, ignoring that is not a rounding error.

---

<a name="6"></a>
## 6. How you know the engine is correct

Three layers, in increasing order of how impressive they are.

### Layer 1: unit tests (81 checks)

`tests/test_order_book.cpp`. Every rule the book claims to implement gets a test
that would fail if the rule broke: time priority, price priority, trades printing
at the *resting* price, partial fills, multi-level sweeps, cancel idempotency,
the modify asymmetry, IOC vs FOK, duplicate id rejection.

### Layer 2: invariant fuzzing (200,000 random operations)

Throw random adds, cancels and market orders at the book, then assert properties
that must hold *no matter what*:

- the book is never crossed (best bid < best ask)
- every level's cached `total_qty` equals the sum of its orders
- no empty level is left in the map
- the id→slot map size equals the count of orders across all levels

Fuzzing finds the state you never thought to write a test for.

### Layer 3: differential testing — the one worth talking about

`python/lobkit/pybook.py` is a **completely independent** implementation of the
same specification: pure Python, dict-of-deques instead of map-of-intrusive-lists,
written without reference to the C++ internals.

`scripts/04_differential_test.py` generates 120,000 deliberately adversarial
events — a narrow price band so levels constantly empty and refill, duplicate
cancels, modifies that cross the spread — and feeds both books the same stream,
comparing every observable after **every single event**: best bid, best ask, top
size each side, level counts, cumulative trades, cumulative volume, and the full
10-level ladder at checkpoints.

```
  events replayed             120,000
  trades (C++)                 65,236
  trades (Python)              65,236
  volume (C++)              6,170,280
  volume (Python)           6,170,280
  mismatches                        0
```

Two independent implementations of the same spec, in different languages with
different data structures, agreeing exactly on a million-plus observations. This
finds a class of bug that unit tests structurally cannot: the case nobody thought
of. It is how exchanges, database engines and compilers are validated, and it is
worth being able to explain.

---

<a name="7"></a>
## 7. Reading a Level-2 book: mid, microprice, imbalance

Given the top of book, three numbers matter.

**Mid** = (bid + ask) / 2. Simple, standard, and slightly wrong.

**Microprice** — the size-weighted mid:

```
microprice = (bid_price * ask_size + ask_price * bid_size) / (bid_size + ask_size)
```

Note the crossing: each price is weighted by the size on the *opposite* side.

The intuition: if there are 5,000 shares bid and only 100 offered, the ask will
be consumed long before the bid is. The next trade is far more likely to happen
at the ask, so fair value sits *closer to the ask*. The microprice captures this;
the mid does not.

Empirically the microprice is a **better short-horizon predictor of the future
mid than the mid itself is**. That is a strong statement — it means the mid is
not a martingale at high frequency, and there is information in the queue sizes.
This is one of the most widely used features in high-frequency trading.

**Order book imbalance**:

```
imbalance = (bid_size - ask_size) / (bid_size + ask_size)   in [-1, +1]
```

Positive means buying pressure. It is the microprice's signal, normalised.

In this project the market maker anchors its quotes to the microprice by default.
Switching to the mid (`use_microprice=False`) is one of the ablations in
`scripts/02_experiments.py` — try it and watch what changes.

---

<a name="8"></a>
## 8. What a market maker does

A market maker posts a bid *and* an ask simultaneously and hopes both get hit.

Buy at $10.00, sell at $10.02, keep $0.02. Do that ten thousand times a day on
small size and the pennies compound. You are being paid for providing
**immediacy**: other people want to trade *right now*, and you are the one
standing there willing to be on the other side.

That is genuinely valuable. Without market makers, a buyer would have to wait for
a seller to show up, and the price you got would depend on your luck in timing.

The strategy in `python/lobkit/strategy.py` is:

```python
reservation_price = reference_price - skew * inventory
bid = floor(reservation_price - half_spread)
ask = ceil (reservation_price + half_spread)
```

Three parameters — reference price, half-spread, skew — and one of them carries
almost all the intelligence.

---

<a name="9"></a>
## 9. The two things that kill market makers

### (a) Inventory risk

Suppose only your bid gets hit, ten times in a row. Now you are long 10,000
shares of a stock you never wanted a view on. If the price drops 1%, you lose
$1,000 — far more than all the spread you earned collecting it.

You did not intend to take a directional bet. You took one anyway, because the
market handed it to you one fill at a time.

**The fix is skew.** When you are long, move *both* quotes down:

- your bid becomes less attractive → you buy less
- your ask becomes more attractive → you sell more

Inventory gets pulled back toward zero automatically, without you deciding
anything. It is one line of code and it is the most important idea in market
making.

The ablation proves it. Turning skew off in `scripts/02_experiments.py`:

| | inventory std dev | max drawdown | Sharpe |
|---|---|---|---|
| with skew | **51 shares** | **$19** | 1,858 |
| no skew | **315 shares** | **$34** | 1,015 |

Six times the inventory risk. Note that PnL actually went *up* slightly without
skew — because in this simulated market, holding inventory happened to pay.
That is exactly the trap: skew costs you a little expected return to buy a large
reduction in risk, and if you only look at PnL you will conclude, wrongly, that
risk management is a waste.

### (b) Adverse selection — the one that really matters

The people trading against you sometimes know something you do not.

If your offer gets lifted right before the price jumps up, you sold too cheap.
Not because you were unlucky, but because the person who bought from you was
*informed*, and they chose to trade with you *precisely because* your price was
stale.

Here is the uncomfortable structure of market making: **you are systematically on
the wrong side of every trade you do.** People trade with you when your price is
wrong in their favour. That is not a bug in your strategy; it is the definition
of providing liquidity.

You cannot avoid it. You can only

1. **charge for it** — quote a wide enough spread that the average fill pays for
   the average adverse move, and
2. **measure it** — which is what markouts do.

### Measuring adverse selection: markouts

For every fill, look at where the mid was `h` seconds *later*:

```
buy  markout = mid(t+h) - fill_price
sell markout = fill_price - mid(t+h)
```

Positive means the market moved your way after you traded.

But a positive markout does **not** mean there is no adverse selection, and this
trips people up. Split it:

```
markout = sign * (mid_(t+h) - fill_price)
        = sign * (mid_t     - fill_price)      <- SPREAD CAPTURE
        + sign * (mid_(t+h) - mid_t)           <- POST-TRADE DRIFT
```

**Spread capture** is the edge you quoted. It is positive by construction for a
passive market maker: you bought below the mid.

**Post-trade drift** is the actual adverse selection: how the market moved
*after* you traded, signed by your direction. It is almost always negative.

This project's baseline run:

```
spread capture           +1.40 cents/share
adverse selection (1s)   -1.05 cents/share
net markout              +0.36 cents/share
maker rebate             +0.20 cents/share
------------------------------------------
net                      +0.56 cents/share
```

That is what real market-making economics looks like. You capture most of the
spread, hand about three quarters of it straight back to informed traders, and
the rebate is a meaningful fraction of what is left. Reporting only the combined
markout hides which of the three is doing the work — and that is the first thing
a desk will ask you.

---

<a name="10"></a>
## 10. Avellaneda–Stoikov, derived in words

The 2008 paper *High-frequency trading in a limit order book* is the standard
academic reference. Being able to explain it is a real differentiator, and it is
much simpler than the stochastic control machinery makes it look.

**The setup.** You hold inventory `q`. The mid price follows a random walk with
volatility `σ`. Your orders fill with probability decaying exponentially in how
far from the mid you quote: `P(fill) ~ A·exp(-k·distance)`. You have exponential
utility with risk aversion `γ`. Where do you quote?

**The two answers:**

```
reservation price  r = S - q·γ·σ²·(T-t)

optimal spread       = γ·σ²·(T-t) + (2/γ)·ln(1 + γ/k)
```

**Reading the reservation price.** This is your *personal* fair value, as
distinct from the market's. It is the mid, adjusted for the fact that you already
hold `q`. If you are long, you genuinely value the asset *less* than the market
does — taking more of it adds to risk you are already carrying. So you shade
down.

The key thing the fixed-skew strategy cannot do: this skew scales with **σ²**. In
a volatile market you de-risk far more aggressively, automatically. That is the
whole argument for the model.

**Reading the spread.** Two terms:

- `γ·σ²·(T-t)` — compensation for inventory risk. More volatility or a longer
  horizon means you demand more.
- `(2/γ)·ln(1 + γ/k)` — the *monopoly pricing* term. Quote wider, earn more per
  fill, get fewer fills. This is the optimum of that trade-off.

Notice the spread does **not** depend on `q` at all. Inventory changes *where you
centre* your quotes, not *how wide* they are. That is a genuinely non-obvious
result and a good thing to be able to state.

**Two adaptations made in the code**, both of which real desks make:

- `(T-t)` is replaced by a fixed horizon. The original model has a hard
  end-of-day at `T` where the trader is forced flat, which makes the skew blow up
  in the final seconds. The steady-state version is what people run.
- `σ` is estimated online rather than assumed known — and *how* you estimate it
  turns out to matter enormously, which is the next section.

---

<a name="11"></a>
## 11. Simulating a market you can trust

Free Level-2 data is scarce (`lobkit/lobster.py` handles the real-data path). But
there is a better reason to simulate: **with a simulator you know the ground
truth.** You know the true fair value at every instant, you can switch individual
effects on and off, and you can prove your backtester measures what you think it
measures before you point it at real data.

### The backbone: Cont–Stoikov–Talreja (2010)

Treat the book as a system of queues driven by three Poisson processes:

1. **Limit orders** arrive `i` ticks from the opposite best quote at rate
   `λ(i) = λ₀ / i^α`. Most activity happens near the touch and decays with
   distance — this power law is one of the most robust empirical facts about real
   books.
2. **Market orders** arrive at rate `μ` and consume the best quote.
3. **Cancellations** at a level occur at rate `θ·q`, proportional to the size
   resting there. Every resting share has its own constant hazard of being pulled.

Everything is a competing exponential clock, simulated with the Gillespie
algorithm: sum the rates, draw the waiting time, draw which clock fired.

### Calibration is not optional

My first attempt had `θ = 0.28`. That looked reasonable in isolation. It was
wrong by a factor of ~35, and it destroyed everything downstream.

Here is the arithmetic that matters. Shares added per second must roughly equal
shares removed per second, or the book either freezes solid or shreds to nothing:

```
added   = λ_total × mean_order_size     ≈ 35.5/s × 120 = 4,260 shares/s
traded  = μ × mean_market_size          ≈ 10/s   × 180 = 1,800 shares/s
cancelled must therefore be             ≈ 2,460 shares/s
        = θ × Q × mean_order_size
=> with a target depth Q ≈ 2,400 shares: θ ≈ 0.014
```

With `θ = 0.28`, cancellations ran at ~900 events/sec against ~35 adds/sec. The
book was permanently one tick deep, the mid whipsawed, and the strategy showed
a fake **−1.29 cents/share** of adverse selection. Every statistic downstream
was measuring the parameter error, not the strategy.

**The lesson**: in a simulation study, a mis-set parameter does not produce an
obviously broken result. It produces a plausible-looking wrong one. Sanity-check
the *simulated market* — depth, spread, event mix, realised volatility — against
what real markets look like, before you look at any strategy result at all.

Volatility got the same treatment. A $100 stock at 20% annualised vol moves
`20%/√252 = 1.26% = 126 ticks` over a 6.5-hour session, so per √second that is
`126/√23400 ≈ 0.8 ticks`. My initial guess of 0.06 was more than ten times too
low.

### Adverse selection has to be *generated*, not assumed

Pure CST flow is symmetric and the mid mean-reverts. A market maker in that world
prints money, and any backtest built on it is a fantasy an interviewer will
demolish in one question.

So the simulator carries a **hidden fair value** `F` following a random walk, and
tilts the order flow toward it:

```
P(next market order is a buy) = sigmoid(κ · (F - mid))
```

When `F` drifts above the mid, buyers appear, they lift your offer, and *then*
the mid follows `F` upward — leaving you short at a bad price. That is adverse
selection arising endogenously from informed flow, not bolted on as a fudge
factor.

Set `informed_kappa=0.0` to turn it off. The ablation table has that row, and the
result is genuinely interesting: PnL *fell* from $1,670 to $308 and drawdown grew
5×. Removing informed flow made things **worse**, which is the opposite of what I
predicted.

Why? Because informed flow makes the mid *track* the fair value, which makes it
partially predictable, and the microprice-anchored strategy exploits that. With
`κ=0` the price is pure noise and the queue-imbalance signal carries no
information. I did not anticipate that. It is in the results anyway, because
reporting only the ablations that confirmed my priors would be the opposite of
science.

### Jumps

A pure diffusion is far too kind. Real market-maker losses come from **discrete
news**: the price gaps, every quote resting on the wrong side gets lifted at the
old price, and you are instantly holding a loser. Jumps are what give the PnL
distribution its fat left tail. Adding a jump every ~4 minutes with 8-tick
standard deviation raised max drawdown **6×** (from $9 to $56) with barely any
change in mean PnL — exactly the signature of tail risk.

---

<a name="12"></a>
## 12. Backtesting without lying to yourself

The naive market-making backtest says *"the price touched my quote, so I got
filled"*. That is wrong, and it is the most common way people produce a beautiful
equity curve that evaporates in production.

It is wrong because of queue position (section 3). Touching your price is not
enough — the entire queue ahead of you must trade first, while your order is
still there.

**This backtester does not model fills at all.** Our orders go into the same C++
book as the simulated crowd's, and the matching engine decides. If a market sell
sweeps down to our bid, the engine fills whoever is at the head of the queue. If
that is us, we get a trade. Queue position is handled correctly because it is
*real*, not approximated.

Measured: our orders join with a mean of **157 shares queued ahead** of them, and
the fill distribution in `results/*_distributions.png` shows exactly which queue
positions actually traded.

### The other things a fill model has to get right

**Fees.** US equity exchanges mostly run *maker-taker*: you are **paid** a rebate
for a passive fill (~$0.0020/share) and **charged** for an aggressive one
(~$0.0030/share). A market maker who ignores the rebate is understating revenue
by, in this backtest, **$2,033 of $5,616 — 36% of total PnL**. On a penny-wide
name the rebate can exceed the entire spread edge.

**Latency.** You cannot requote instantly. `min_requote_interval_s` (default 50
ms) stands in for network latency and exchange message-rate limits. The 500 ms
ablation cuts fills by 58%.

**Not churning your quote.** If the desired price has not changed, leave the
order alone. Cancel-and-replace sends you to the back of the queue and throws
away the one asset you accumulate for free. `_sync_quotes` only replaces when
the target price has moved by more than a tolerance.

**Pre-trade risk checks.** Independent of the strategy, the order-management
layer refuses any quote absurdly far from the market or any order that would
breach a hard position limit. Section 16 explains why this is not optional.

---

<a name="13"></a>
## 13. The metrics, and what each one is protecting you from

A single headline number is never enough. Each metric below exists because
without it, a specific kind of self-deception is possible.

| Metric | The lie it prevents |
|---|---|
| **Fill rate** | "My strategy is great" — when it barely traded. |
| **Participation %** | "Look at this PnL" — when you took 30% of all volume and a real market would have repriced around you. |
| **Inventory std dev** | "Good Sharpe" — from a strategy that is quietly a directional bet. |
| **Buy/sell ratio** | Same, from the other direction. Should be ≈ 1.0. |
| **Max drawdown** | A smooth-looking curve with one catastrophic day in it. |
| **Spread capture** | "It's profitable" — without knowing *why*. |
| **Adverse selection** | The most important one. See section 9. |
| **Queue position at entry** | Fills that were only possible because the model let you jump the queue. |
| **Risk rejections** | A strategy that only works because nothing stopped it doing something insane. |

### On Sharpe, honestly

The annualised Sharpe this backtest reports is **~1,584**. That number is not
real, and the code says so in the output.

It is inflated because:

- **The simulated crowd does not react to us.** We take ~9% of all volume with no
  competitive response. A real market would have someone undercutting our quote
  within milliseconds.
- Scaling per-second PnL by `√(seconds per year)` assumes i.i.d. returns.
  High-frequency PnL is autocorrelated, which biases the annualised figure
  upward.
- One instrument, one venue, no exchange outages, no latency jitter, no regime
  changes, no capital constraints.

The more defensible number is the **cross-run Sharpe** in
`scripts/02_experiments.py`: run N independent simulations with different seeds,
treat each as one independent observation, and compute

```
Sharpe = mean(block PnL) / stdev(block PnL) × √(blocks per year)
```

This makes no i.i.d. assumption about second-to-second PnL. It only assumes the
runs are independent, which they are by construction. It comes out at **~2,165** —
still far too high, for the structural reasons above.

**Say this out loud in an interview.** "My backtest reports a Sharpe in the
thousands; here is why that number is not real, and here is the more defensible
one, which is also too high, and here is what would have to be in the model
before I believed it" is a *much* stronger answer than a plausible-looking 3.2.
Anyone senior has seen a hundred backtests with a Sharpe of 3.2 and knows most of
them are broken.

---

<a name="14"></a>
## 14. Everything that is still wrong with this backtest

Know these before someone else finds them.

1. **No market impact from our own presence.** The biggest one. The simulated
   crowd's order flow is completely unaffected by our quotes. In reality, posting
   inside the spread invites others to undercut you, and taking 9% of volume
   would change the market's behaviour. This is why participation % is reported
   prominently.

2. **No competing market makers.** When we improve the touch we are often alone
   at that price and get 100% of the flow there. Reality has dozens of
   competitors racing to the same spot.

3. **Zero latency jitter.** The requote interval is a constant. Real latency is a
   random variable with a nasty tail, and the tail is exactly when it hurts.

4. **One venue.** US equities fragment across a dozen exchanges with routing,
   locked/crossed markets and Reg NMS. Ignored entirely.

5. **No queue-position decay from partial fills ahead of us** beyond what the
   book naturally does — correct here, but real books have hidden and iceberg
   orders that this model has none of.

6. **Perfect market data.** No feed gaps, no out-of-order messages, no gateway
   rejects.

7. **A single asset with no cross-asset hedge.** A real desk hedges index risk
   rather than sitting on it.

8. **The simulator is a model.** The CST framework is a good caricature, not
   reality. Real books have long-memory order flow, intraday seasonality (a
   U-shaped volume curve), and auctions at the open and close.

Being able to list these unprompted is worth more than any number in the results.

---

<a name="15"></a>
## 15. The C++/Python boundary

Writing "exposed a C++ engine to Python with pybind11" on a CV is easy. The
interesting question is what the boundary *costs*, and it is a lot.

A pybind11 call is roughly 100–300 ns. Our `add_limit` is ~35 ns. So calling the
engine once per event from Python means spending more time marshalling arguments
than matching orders.

Measured on the same 300,000-event workload:

| approach | ns/event | events/sec |
|---|---|---|
| `replay_batch()` — **one** boundary crossing | **111** | **9,030,881** |
| per-event calls — 300,000 crossings | 1,178 | 849,254 |

**10.6× faster** for identical work. About **1,067 ns/event** of the per-event
timing is pure boundary overhead — argument conversion, reference counting, the
GIL. The matching itself is the small part.

The lesson: **keep tight loops on the C++ side and cross the boundary once per
batch**. In this project, the throughput benchmark uses `replay_batch`, while the
backtest — which must make a decision after every event — necessarily pays the
per-event cost and is therefore bounded by Python, not by the order book.
`replay_batch` also releases the GIL, since it touches no Python objects.

There is a second, subtler trap here, and I hit it. pybind11's `std::vector`
caster **copies**. My first version exposed `trades()` returning the whole trade
buffer, and the backtest called it once per event — rebuilding a Python list of
*every trade so far*, every time. That turned an O(1) read into O(n) and the
whole backtest into O(n²). Adding `trades_since(cursor)`, which copies only the
new trades, made the backtest **35× faster** (30k events in 22 s → 60k events in
1.7 s).

Neither bug produced a wrong answer. Both were invisible without measuring.

---

<a name="16"></a>
## 16. The bugs I hit, and what each one taught

These are worth more in an interview than the features. Everyone's project
"works". Being able to say "here is a subtle thing that broke and here is what it
taught me" is what separates a real project from a tutorial.

### Bug 1: the benchmark that measured nothing

`BM_Cancel` reported 7.5 ns and looked fantastic. Google Benchmark chooses
iteration counts at *runtime* — it asked for 24 million iterations, and I had
pre-built only 2 million orders to cancel. Iterations 2,000,001 onward cancelled
*nothing*, and `cancel()` on a missing id is a single failed hash lookup. I was
proudly reporting the speed of doing nothing.

**Fix**: work in chunks, refilling state under `PauseTiming()`, and add a counter
(`cancels_that_hit`) that must equal the iteration count. **A benchmark needs its
own assertion.** Same bug in `BM_Market_OneFill`.

### Bug 2: O(n²) hidden behind a clean API

Covered in section 15. `trades()` looked like a getter. It was a full copy across
the language boundary.

**Lesson**: know what your FFI layer does with each type. "It's just a getter" is
not a statement about cost.

### Bug 3: the spread capture that was negative

Early runs showed spread capture of **−0.27 cents/share** — meaning the market
maker was, on average, buying *above* the mid. Structurally impossible for a
passive strategy.

The cause: I recorded `mid_at_fill` as the mid *after* the event. But the event
that fills your bid is a market sell, which *also* pushes the mid down. So the
comparison double-counted the trade's own price impact and made every passive buy
look like it happened above the mid.

**Fix**: capture the mid *before* the event and use that as the reference.

**Lesson**: when a metric has an impossible sign, do not tune the strategy —
question the measurement. A one-line timing error flipped the headline result of
the entire project.

### Bug 4: the parameter that was 35× wrong

The cancellation rate, covered in section 11. It did not crash. It produced a
plausible, entirely wrong market.

### Bug 5: the feedback loop that produced a bid of −402,950

The best one. Avellaneda–Stoikov crashed with an invalid price. The chain:

1. The online volatility estimate spiked.
2. `reservation = ref − q·γ·σ²·horizon` ran away.
3. The strategy posted a bid at −402,950. My price clamp only capped quotes from
   *above* (`min(bid, best_ask - 1)`), so an absurdly *low* bid passed straight
   through.
4. That bid became the **best bid**.
5. The flow simulator anchors its sell orders to the best bid — so it started
   placing sells at −402,949, which crossed and **wiped out the entire bid side**.
6. The mid went negative, the volatility estimate spiked further, and round it
   went.

One unbounded number destroyed the entire market, through a feedback path I had
not imagined.

**Two fixes, both of which real systems have:**

- A **price band** in the strategy: no quote further than N ticks from the
  reference.
- An independent **pre-trade risk check** in the order-management layer, which
  never trusts the model: reject any order far from the market or breaching a
  hard position limit.

Exchanges enforce their own price bands and limit-up/limit-down halts for exactly
this reason. **An unbounded price is never a valid order, however confident the
model is.**

### Bug 6: measuring noise and calling it volatility

Once A–S was stable, it quoted **50 ticks wide** and stopped market making.

The volatility estimate was ~5 ticks/√s against a true 0.6 — an 8× overestimate.
The cause is a genuine and important effect: the observed mid is the efficient
price **plus microstructure noise** (bid-ask bounce, one order flipping the
touch). That noise does not accumulate over time, but squared returns get divided
by an ever-smaller `dt` as you sample faster — so the realised-volatility
estimate *diverges* as your sampling rate rises.

This is the **volatility signature plot**, and `scripts/02_experiments.py`
reproduces it exactly:

```
  sampling interval    estimated vol    × true
        0.05 s             5.668          9.4
        0.1  s             4.754          7.9
        0.5  s             2.550          4.3
        1    s             1.867          3.1
        5    s             1.105          1.8
       30    s             0.749          1.2
       60    s             0.540          0.9
                    true value: 0.60
```

A textbook curve, produced by accident, from a strategy bug.

**Fix**: sample the mid on a fixed 5-second grid instead of every event. This is
exactly why practitioners compute realised variance at 5–30 second intervals
rather than tick by tick.

**Lesson**: *how often you sample* is a modelling decision with real consequences,
not an implementation detail.

---

## Where to go next

- `docs/INTERVIEW.md` — the questions you will be asked, and how to answer them.
- The code. Every file opens with a long comment written for someone who has read
  this document.
- Extensions worth doing, roughly in order of value: multi-level quoting; a
  competing market maker in the simulator; latency as a random variable; an
  imbalance-based alpha signal on top of the microprice; hedging with a
  correlated instrument; and running the whole thing on real LOBSTER data
  (`scripts/05_lobster_real_data.py` is already written for it).
