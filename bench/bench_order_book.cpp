// bench_order_book.cpp -- Google Benchmark micro-benchmarks.
//
// HOW TO READ THE OUTPUT
// ----------------------
//   Time        wall-clock nanoseconds per iteration
//   CPU         CPU nanoseconds per iteration
//   Iterations  how many times the loop ran (chosen automatically for stability)
//   items/s     our own counter: operations per second
//
// THE TRAP THIS FILE AVOIDS
// -------------------------
// Google Benchmark decides how many iterations to run AT RUNTIME -- it keeps
// going until the timings are stable, which can be 50 million iterations. If
// you pre-build 2 million orders to cancel and the harness asks for 50 million
// iterations, iterations 2,000,001 onward cancel nothing. `cancel()` on a
// missing id is a single failed hash lookup, so you would proudly report a
// 7 ns cancel that never actually cancelled anything.
//
// The fix used throughout: work in CHUNKS. Consume kChunk items, then
// PauseTiming(), rebuild the state, ResumeTiming(). Pausing costs a few
// hundred nanoseconds but it happens once per 200,000 iterations, so it adds
// under 0.002 ns per operation -- invisible.
//
// WHY benchmark::DoNotOptimize
// ----------------------------
// -O2 deletes code whose result is never used. DoNotOptimize tells the
// compiler "pretend something reads this", so it cannot delete the thing we
// are trying to measure.
#include <benchmark/benchmark.h>

#include <random>
#include <vector>

#include "lob/order_book.hpp"

using namespace lob;

namespace {

constexpr Price kMid       = 100000;   // $1000.00 in $0.01 ticks
constexpr Price kTickRange = 20;       // price levels populated on each side
constexpr std::size_t kChunk = 200000; // iterations between state rebuilds

// Build a book with `orders_per_side` resting orders spread over kTickRange
// levels each side, leaving a 2-tick spread. Fixed seed => comparable runs.
void build_book(OrderBook& book, int orders_per_side, OrderId& next_id) {
    std::mt19937_64 rng(12345);
    std::uniform_int_distribution<int> lvl(0, kTickRange - 1);
    std::uniform_int_distribution<int> sz(1, 500);
    for (int i = 0; i < orders_per_side; ++i) {
        book.add_limit(next_id++, Side::Buy,  kMid - 1 - lvl(rng), sz(rng), i);
        book.add_limit(next_id++, Side::Sell, kMid + 1 + lvl(rng), sz(rng), i);
    }
}

}  // namespace

// ---------------------------------------------------------------------------
// 1. Add a passive limit order that cannot match -- the most common event in
//    any real market. Arg(0) = resting orders per side already in the book, so
//    we can see how the cost moves with book size (cache pressure).
// ---------------------------------------------------------------------------
static void BM_AddLimit_Passive(benchmark::State& state) {
    const int depth = static_cast<int>(state.range(0));
    OrderBook book(1u << 20);
    OrderId seed_id = 1;
    build_book(book, depth, seed_id);

    const OrderId base = seed_id;
    OrderId id = base;
    std::size_t k = 0;

    for (auto _ : state) {
        if (k == kChunk) {
            state.PauseTiming();
            for (OrderId j = base; j < id; ++j) book.cancel(j);  // restore state
            id = base;
            k = 0;
            book.clear_trades();
            state.ResumeTiming();
        }
        // 5 ticks inside the bid: guaranteed passive, never crosses.
        auto rep = book.add_limit(id++, Side::Buy, kMid - 5, 100, 0);
        benchmark::DoNotOptimize(rep);
        ++k;
    }
    state.SetItemsProcessed(state.iterations());
    state.counters["book_orders"] = static_cast<double>(book.live_orders());
}
BENCHMARK(BM_AddLimit_Passive)->Arg(1000)->Arg(50000);

// ---------------------------------------------------------------------------
// 2. Cancel. In US equities well over 90% of orders end this way, so this is
//    arguably the most important single number here. We verify with a counter
//    that every timed cancel actually removed an order.
// ---------------------------------------------------------------------------
static void BM_Cancel(benchmark::State& state) {
    OrderBook book(1u << 21);
    OrderId id = 1;
    build_book(book, 20000, id);

    std::vector<OrderId> victims(kChunk);
    auto refill = [&] {
        for (std::size_t i = 0; i < kChunk; ++i) {
            book.add_limit(id, Side::Buy, kMid - 5, 100, 0);
            victims[i] = id++;
        }
    };
    refill();

    std::size_t k = 0;
    std::uint64_t hits = 0;
    for (auto _ : state) {
        if (k == kChunk) {
            state.PauseTiming();
            refill();
            k = 0;
            state.ResumeTiming();
        }
        hits += book.cancel(victims[k++]) ? 1 : 0;
    }
    state.SetItemsProcessed(state.iterations());
    // Sanity: must equal Iterations. If it does not, the benchmark is lying.
    state.counters["cancels_that_hit"] = static_cast<double>(hits);
}
BENCHMARK(BM_Cancel);

// ---------------------------------------------------------------------------
// 3. Modify, size DOWN at the same price -> handled in place, queue priority
//    preserved. This is the cheapest write the engine has: no map lookup by
//    price beyond one find(), no relink.
// ---------------------------------------------------------------------------
static void BM_Modify_ShrinkInPlace(benchmark::State& state) {
    OrderBook book(1u << 20);
    OrderId id = 1;
    build_book(book, 10000, id);

    const OrderId target = id++;
    constexpr Quantity kStart = 1'000'000'000;
    book.add_limit(target, Side::Buy, kMid - 5, kStart, 0);

    Quantity q = kStart;
    std::size_t k = 0;
    for (auto _ : state) {
        if (k == kChunk) {                 // re-inflate so q never hits zero
            state.PauseTiming();
            book.cancel(target);
            book.add_limit(target, Side::Buy, kMid - 5, kStart, 0);
            q = kStart;
            k = 0;
            state.ResumeTiming();
        }
        bool ok = book.modify(target, kMid - 5, --q, 0);
        benchmark::DoNotOptimize(ok);
        ++k;
    }
    state.SetItemsProcessed(state.iterations());
}
BENCHMARK(BM_Modify_ShrinkInPlace);

// ---------------------------------------------------------------------------
// 4. Modify with a price change -> cancel + re-add, queue priority LOST.
//    Expect roughly cancel + add. The gap between this and #3 is exactly why
//    exchanges' in-place-shrink rule matters to a market maker.
// ---------------------------------------------------------------------------
static void BM_Modify_Reprice(benchmark::State& state) {
    OrderBook book(1u << 20);
    OrderId id = 1;
    build_book(book, 10000, id);

    const OrderId target = id++;
    book.add_limit(target, Side::Buy, kMid - 5, 100, 0);

    bool flip = false;
    for (auto _ : state) {
        flip = !flip;
        bool ok = book.modify(target, flip ? kMid - 6 : kMid - 5, 100, 0);
        benchmark::DoNotOptimize(ok);
    }
    state.SetItemsProcessed(state.iterations());
}
BENCHMARK(BM_Modify_Reprice);

// ---------------------------------------------------------------------------
// 5. Aggressive market order consuming exactly one resting order: the matching
//    path (build a Trade, unlink, release the pool slot, erase from the id map).
// ---------------------------------------------------------------------------
static void BM_Market_OneFill(benchmark::State& state) {
    OrderBook book(1u << 21);
    OrderId id = 1;
    build_book(book, 5000, id);

    auto refill = [&] {
        for (std::size_t i = 0; i < kChunk; ++i)
            book.add_limit(id++, Side::Sell, kMid + 1, 100, 0);
    };
    refill();

    std::size_t k = 0;
    std::uint64_t filled = 0;
    for (auto _ : state) {
        if (k == kChunk) {
            state.PauseTiming();
            refill();
            book.clear_trades();
            k = 0;
            state.ResumeTiming();
        }
        auto rep = book.add_market(id++, Side::Buy, 100, 0);
        filled += static_cast<std::uint64_t>(rep.filled_qty);
        ++k;
    }
    state.SetItemsProcessed(state.iterations());
    // Sanity: should equal 100 * Iterations.
    state.counters["qty_actually_filled"] = static_cast<double>(filled);
}
BENCHMARK(BM_Market_OneFill);

// ---------------------------------------------------------------------------
// 6. Market order that SWEEPS several levels. Each extra level costs a map
//    erase plus a fresh begin(), so this shows the cost of a liquidity event.
// ---------------------------------------------------------------------------
static void BM_Market_SweepLevels(benchmark::State& state) {
    OrderBook book(1u << 21);
    OrderId id = 1;

    // Rebuild a 5-level ladder of 100 lots each, then sweep 500.
    auto rebuild = [&] {
        for (std::size_t i = 0; i < kChunk / 5; ++i)
            for (Price d = 1; d <= 5; ++d)
                book.add_limit(id++, Side::Sell, kMid + d, 100, 0);
    };
    rebuild();

    std::size_t k = 0;
    for (auto _ : state) {
        if (k == kChunk / 5) {
            state.PauseTiming();
            rebuild();
            book.clear_trades();
            k = 0;
            state.ResumeTiming();
        }
        auto rep = book.add_market(id++, Side::Buy, 500, 0);
        benchmark::DoNotOptimize(rep);
        ++k;
    }
    state.SetItemsProcessed(state.iterations());
}
BENCHMARK(BM_Market_SweepLevels);

// ---------------------------------------------------------------------------
// 7. THROUGHPUT: a realistic mixed event stream replayed through the batched
//    interface. THIS is the number to put on a resume, because it reflects the
//    real mix rather than one operation in isolation.
//    Mix modelled on US equities: 55% add, 35% cancel, 10% aggressive.
// ---------------------------------------------------------------------------
static void BM_Replay_MixedFlow(benchmark::State& state) {
    const std::size_t n = static_cast<std::size_t>(state.range(0));

    std::vector<std::uint8_t> types(n), sides(n);
    std::vector<OrderId>      ids(n);
    std::vector<Price>        prices(n);
    std::vector<Quantity>     qtys(n);
    std::vector<Timestamp>    ts(n);

    std::mt19937_64 rng(999);
    std::uniform_int_distribution<int> pct(0, 99);
    std::uniform_int_distribution<int> lvl(0, kTickRange - 1);
    std::uniform_int_distribution<int> sz(1, 500);

    std::vector<OrderId> live;
    live.reserve(n);
    OrderId next = 1;

    for (std::size_t i = 0; i < n; ++i) {
        const int r = pct(rng);
        const bool buy = (rng() & 1u) != 0;
        sides[i] = buy ? 0 : 1;
        qtys[i]  = sz(rng);
        ts[i]    = static_cast<Timestamp>(i);

        if (r < 55 || live.empty()) {
            types[i]  = static_cast<std::uint8_t>(EventType::AddLimit);
            ids[i]    = next++;
            prices[i] = buy ? kMid - 1 - lvl(rng) : kMid + 1 + lvl(rng);
            live.push_back(ids[i]);
        } else if (r < 90) {
            types[i] = static_cast<std::uint8_t>(EventType::Cancel);
            const std::size_t k = rng() % live.size();
            ids[i] = live[k];
            live[k] = live.back();
            live.pop_back();
        } else {
            types[i]  = static_cast<std::uint8_t>(EventType::AddMarket);
            ids[i]    = next++;
            prices[i] = 0;
            qtys[i]   = 1 + (sz(rng) % 200);
        }
    }

    std::uint64_t trades = 0;
    for (auto _ : state) {
        state.PauseTiming();
        OrderBook book(1u << 20);
        book.reserve_trades(n / 4);
        state.ResumeTiming();

        trades += book.replay(types.data(), ids.data(), sides.data(), prices.data(),
                              qtys.data(), ts.data(), n);
    }
    state.SetItemsProcessed(static_cast<std::int64_t>(state.iterations() * n));
    state.counters["trades_per_run"] =
        static_cast<double>(trades) / static_cast<double>(state.iterations());
}
BENCHMARK(BM_Replay_MixedFlow)->Arg(1000000)->Unit(benchmark::kMillisecond);

// ---------------------------------------------------------------------------
// 8. Read path. A strategy re-reads top of book after every event, so these
//    have to be essentially free.
// ---------------------------------------------------------------------------
static void BM_TopOfBook(benchmark::State& state) {
    OrderBook book(1u << 20);
    OrderId id = 1;
    build_book(book, 50000, id);
    for (auto _ : state) {
        double mp = 0;
        book.microprice(mp);
        benchmark::DoNotOptimize(mp);
    }
    state.SetItemsProcessed(state.iterations());
}
BENCHMARK(BM_TopOfBook);

static void BM_Depth10(benchmark::State& state) {
    OrderBook book(1u << 20);
    OrderId id = 1;
    build_book(book, 50000, id);
    for (auto _ : state) {
        auto d = book.depth(Side::Buy, 10);
        benchmark::DoNotOptimize(d);
    }
    state.SetItemsProcessed(state.iterations());
}
BENCHMARK(BM_Depth10);

BENCHMARK_MAIN();
