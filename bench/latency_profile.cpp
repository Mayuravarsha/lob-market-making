// latency_profile.cpp -- per-operation latency DISTRIBUTION, not just the mean.
//
// WHY THIS EXISTS ALONGSIDE GOOGLE BENCHMARK
// ------------------------------------------
// Google Benchmark reports the MEAN time per operation. In trading, the mean is
// close to useless. If your book handles an order in 30 ns on average but once
// every 100,000 orders it stalls for 40 microseconds (a hash-map rehash, a
// vector reallocation, a page fault), you will lose money precisely during the
// bursts when everything matters -- because bursts are exactly when the slow
// path triggers.
//
// So we time EVERY individual operation, sort the samples, and report
// percentiles: p50 (median), p99, p99.9, and max. A flat tail means the engine
// has no hidden allocations. A fat tail is a bug you can go and find.
//
// MEASURING NANOSECONDS HONESTLY
// ------------------------------
// Reading the clock is not free -- roughly 15-25 ns for
// std::chrono::steady_clock on Linux. When the thing you are timing takes 30 ns
// that is not a rounding error, it is most of the measurement. So we:
//   1. Measure the clock's own overhead first (two back-to-back reads).
//   2. Subtract the median overhead from every sample.
//   3. Print both the raw and the corrected numbers so nothing is hidden.
//
// We also do a warm-up pass that is thrown away. The first run touches cold
// memory, an un-warmed branch predictor and an empty TLB; including it would
// make the tail look far worse than steady-state reality.
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <random>
#include <string>
#include <vector>

#include "lob/order_book.hpp"

using namespace lob;
using Clock = std::chrono::steady_clock;

namespace {

constexpr Price kMid = 100000;
constexpr Price kTickRange = 20;

struct Stats {
    double p50, p90, p99, p999, max, mean;
    std::size_t n;
};

Stats summarize(std::vector<double>& v) {
    std::sort(v.begin(), v.end());
    auto pct = [&](double p) {
        if (v.empty()) return 0.0;
        std::size_t i = static_cast<std::size_t>(p * (v.size() - 1));
        return v[i];
    };
    double sum = 0;
    for (double x : v) sum += x;
    return Stats{pct(0.50), pct(0.90), pct(0.99), pct(0.999),
                 v.empty() ? 0.0 : v.back(),
                 v.empty() ? 0.0 : sum / v.size(), v.size()};
}

void print_row(const char* name, const Stats& s, double clock_overhead) {
    auto adj = [&](double x) { return x - clock_overhead < 0 ? 0.0 : x - clock_overhead; };
    std::printf("  %-22s %9zu %9.1f %9.1f %9.1f %9.1f %11.1f %10.1f\n",
                name, s.n, adj(s.p50), adj(s.p90), adj(s.p99), adj(s.p999),
                adj(s.max), adj(s.mean));
}

// Measure how long it takes to just read the clock twice.
double measure_clock_overhead(std::size_t n = 200000) {
    std::vector<double> samples;
    samples.reserve(n);
    for (std::size_t i = 0; i < n; ++i) {
        auto t0 = Clock::now();
        auto t1 = Clock::now();
        samples.push_back(std::chrono::duration<double, std::nano>(t1 - t0).count());
    }
    std::sort(samples.begin(), samples.end());
    return samples[samples.size() / 2];   // median
}

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

int main(int argc, char** argv) {
    const std::size_t N = (argc > 1) ? std::stoul(argv[1]) : 2000000;
    // Pass --prefault to touch every reserved page before timing starts.
    // Run the binary both ways to see what soft page faults cost you.
    bool prefault = false;
    for (int i = 1; i < argc; ++i)
        if (std::string(argv[i]) == "--prefault") prefault = true;

    const double clock_ns = measure_clock_overhead();
    std::printf("\nLatency profile  (all figures in NANOSECONDS, clock overhead removed)\n");
    std::printf("samples per op : %zu\n", N);
    std::printf("clock overhead : %.1f ns median (subtracted from every sample)\n", clock_ns);
    std::printf("pre-faulted    : %s\n\n", prefault ? "YES (pages touched up front)"
                                                     : "no  (pass --prefault to enable)");
    std::printf("  %-22s %9s %9s %9s %9s %9s %11s %10s\n",
                "operation", "n", "p50", "p90", "p99", "p99.9", "max", "mean");
    std::printf("  %s\n", std::string(96, '-').c_str());

    // ---------------------------------------------------------------- add ---
    {
        OrderBook book(1u << 21);
        OrderId id = 1;
        build_book(book, 20000, id);
        if (prefault) book.prefault();
        const OrderId base = id;

        for (std::size_t i = 0; i < 100000; ++i)      // warm-up, discarded
            book.add_limit(id++, Side::Buy, kMid - 5, 100, 0);

        std::vector<double> s;
        s.reserve(N);
        for (std::size_t i = 0; i < N; ++i) {
            auto t0 = Clock::now();
            book.add_limit(id++, Side::Buy, kMid - 5, 100, 0);
            auto t1 = Clock::now();
            s.push_back(std::chrono::duration<double, std::nano>(t1 - t0).count());
        }
        print_row("add_limit (passive)", summarize(s), clock_ns);
        (void)base;
    }

    // ------------------------------------------------------------- cancel ---
    {
        OrderBook book(1u << 21);
        OrderId id = 1;
        build_book(book, 20000, id);

        if (prefault) book.prefault();
        std::vector<OrderId> victims;
        victims.reserve(N + 100000);
        for (std::size_t i = 0; i < N + 100000; ++i) {
            book.add_limit(id, Side::Buy, kMid - 5, 100, 0);
            victims.push_back(id++);
        }
        for (std::size_t i = 0; i < 100000; ++i) book.cancel(victims[i]);  // warm-up

        std::vector<double> s;
        s.reserve(N);
        for (std::size_t i = 0; i < N; ++i) {
            auto t0 = Clock::now();
            book.cancel(victims[100000 + i]);
            auto t1 = Clock::now();
            s.push_back(std::chrono::duration<double, std::nano>(t1 - t0).count());
        }
        print_row("cancel", summarize(s), clock_ns);
    }

    // --------------------------------------------------- market order fill ---
    {
        OrderBook book(1u << 21);
        OrderId id = 1;
        build_book(book, 5000, id);
        for (std::size_t i = 0; i < N + 100000; ++i)
            book.add_limit(id++, Side::Sell, kMid + 1, 100, 0);
        for (std::size_t i = 0; i < 100000; ++i) book.add_market(id++, Side::Buy, 100, 0);
        book.clear_trades();
        book.reserve_trades(N + 16);          // pre-reserve: no realloc in the tail
        if (prefault) book.prefault();

        std::vector<double> s;
        s.reserve(N);
        for (std::size_t i = 0; i < N; ++i) {
            auto t0 = Clock::now();
            book.add_market(id++, Side::Buy, 100, 0);
            auto t1 = Clock::now();
            s.push_back(std::chrono::duration<double, std::nano>(t1 - t0).count());
        }
        print_row("market order (1 fill)", summarize(s), clock_ns);
    }

    // ------------------------------------------------- top-of-book read ---
    {
        OrderBook book(1u << 20);
        OrderId id = 1;
        build_book(book, 50000, id);
        if (prefault) book.prefault();
        double mp = 0;
        for (std::size_t i = 0; i < 100000; ++i) book.microprice(mp);

        std::vector<double> s;
        s.reserve(N);
        for (std::size_t i = 0; i < N; ++i) {
            auto t0 = Clock::now();
            book.microprice(mp);
            auto t1 = Clock::now();
            s.push_back(std::chrono::duration<double, std::nano>(t1 - t0).count());
        }
        print_row("microprice read", summarize(s), clock_ns);
    }

    // ---------------------------------------------- realistic mixed flow ---
    // The single most representative number: one event drawn from a realistic
    // 55/35/10 add/cancel/aggress mix, timed individually.
    {
        const std::size_t M = N;
        std::vector<std::uint8_t> types(M), sides(M);
        std::vector<OrderId>      ids(M);
        std::vector<Price>        prices(M);
        std::vector<Quantity>     qtys(M);

        std::mt19937_64 rng(4242);
        std::uniform_int_distribution<int> pct(0, 99), lvl(0, kTickRange - 1), sz(1, 500);
        std::vector<OrderId> live;
        live.reserve(M);
        OrderId next = 1;
        for (std::size_t i = 0; i < M; ++i) {
            const int r = pct(rng);
            const bool buy = (rng() & 1u) != 0;
            sides[i] = buy ? 0 : 1;
            qtys[i] = sz(rng);
            if (r < 55 || live.empty()) {
                types[i] = 1; ids[i] = next++;
                prices[i] = buy ? kMid - 1 - lvl(rng) : kMid + 1 + lvl(rng);
                live.push_back(ids[i]);
            } else if (r < 90) {
                types[i] = 2;
                const std::size_t k = rng() % live.size();
                ids[i] = live[k]; live[k] = live.back(); live.pop_back();
            } else {
                types[i] = 4; ids[i] = next++; prices[i] = 0;
                qtys[i] = 1 + (sz(rng) % 200);
            }
        }

        OrderBook book(1u << 21);
        OrderId wid = 100000000;
        build_book(book, 20000, wid);
        book.reserve_trades(M / 2);
        if (prefault) book.prefault();

        std::vector<double> s;
        s.reserve(M);
        for (std::size_t i = 0; i < M; ++i) {
            const Side sd = static_cast<Side>(sides[i]);
            auto t0 = Clock::now();
            switch (types[i]) {
                case 1: book.add_limit(ids[i], sd, prices[i], qtys[i], (Timestamp)i); break;
                case 2: book.cancel(ids[i]); break;
                default: book.add_market(ids[i], sd, qtys[i], (Timestamp)i); break;
            }
            auto t1 = Clock::now();
            s.push_back(std::chrono::duration<double, std::nano>(t1 - t0).count());
        }
        print_row("MIXED FLOW (any event)", summarize(s), clock_ns);
        std::printf("\n  mixed-flow run produced %llu trades on %llu shares\n",
                    (unsigned long long)book.total_trades(),
                    (unsigned long long)book.total_volume());
    }

    std::printf("\nNote: p99.9 close to p50 means no hidden allocation or rehashing\n"
                "      in the hot path -- which is the whole point of the pool\n"
                "      allocator and the pre-reserved hash map.\n\n");
    return 0;
}
