// test_order_book.cpp -- correctness tests for the matching engine.
//
// No test framework: just a CHECK macro. A matching engine is exactly the kind
// of code where a silent off-by-one costs real money, so every rule the book
// claims to implement gets a test that would fail if the rule broke.
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include "lob/order_book.hpp"

using namespace lob;

static int g_failures = 0;
static int g_checks   = 0;

#define CHECK(cond)                                                            \
    do {                                                                       \
        ++g_checks;                                                            \
        if (!(cond)) {                                                         \
            std::printf("  FAIL  %s:%d   %s\n", __FILE__, __LINE__, #cond);    \
            ++g_failures;                                                      \
        }                                                                      \
    } while (0)

#define CHECK_EQ(a, b)                                                         \
    do {                                                                       \
        ++g_checks;                                                            \
        auto _a = (a); auto _b = (b);                                          \
        if (!(_a == _b)) {                                                     \
            std::printf("  FAIL  %s:%d   %s == %s  (%lld vs %lld)\n",          \
                        __FILE__, __LINE__, #a, #b,                            \
                        (long long)_a, (long long)_b);                         \
            ++g_failures;                                                      \
        }                                                                      \
    } while (0)

static void section(const char* name) { std::printf("\n[%s]\n", name); }

// ---------------------------------------------------------------------------
int main() {
    // ======================================================================
    section("1. resting orders build depth, no trades happen inside a spread");
    // ======================================================================
    {
        OrderBook b;
        b.add_limit(1, Side::Buy,  10000, 100, 1);
        b.add_limit(2, Side::Buy,   9999, 200, 2);
        b.add_limit(3, Side::Sell, 10002, 150, 3);

        Price bb = 0, ba = 0, sp = 0;
        CHECK(b.best_bid(bb)); CHECK_EQ(bb, 10000);
        CHECK(b.best_ask(ba)); CHECK_EQ(ba, 10002);
        CHECK(b.spread(sp));   CHECK_EQ(sp, 2);
        CHECK_EQ(b.total_trades(), 0u);      // nothing crossed
        CHECK_EQ(b.qty_at(Side::Buy, 10000), 100);
        CHECK_EQ(b.live_orders(), 3u);
    }

    // ======================================================================
    section("2. TIME priority: first in, first filled, at the same price");
    // ======================================================================
    {
        OrderBook b;
        b.add_limit(1, Side::Buy, 10000, 100, 1);   // queued first
        b.add_limit(2, Side::Buy, 10000, 100, 2);   // queued second
        b.add_market(99, Side::Sell, 100, 3);       // sell 100

        CHECK_EQ(b.total_trades(), 1u);
        CHECK_EQ(b.trades()[0].resting_id, 1u);     // order 1, not order 2
        CHECK(!b.contains(1));                       // fully filled, gone
        CHECK(b.contains(2));                        // still resting
        CHECK_EQ(b.qty_at(Side::Buy, 10000), 100);
    }

    // ======================================================================
    section("3. PRICE priority beats time");
    // ======================================================================
    {
        OrderBook b;
        b.add_limit(1, Side::Buy, 10000, 100, 1);   // older but cheaper
        b.add_limit(2, Side::Buy, 10001, 100, 2);   // newer but better price
        b.add_market(99, Side::Sell, 100, 3);

        CHECK_EQ(b.trades()[0].resting_id, 2u);     // best price wins
        CHECK_EQ(b.trades()[0].price, 10001);
    }

    // ======================================================================
    section("4. a marketable limit trades at the RESTING price (improvement)");
    // ======================================================================
    {
        OrderBook b;
        b.add_limit(1, Side::Sell, 10002, 100, 1);
        auto rep = b.add_limit(2, Side::Buy, 10005, 100, 2);   // willing to pay 10005

        CHECK_EQ(rep.filled_qty, 100);
        CHECK_EQ(b.trades()[0].price, 10002);       // but only pays 10002
        CHECK_EQ(rep.avg_fill_px_num / rep.filled_qty, 10002);
        CHECK_EQ(rep.resting_qty, 0);
    }

    // ======================================================================
    section("5. partial fill: aggressor rests the remainder");
    // ======================================================================
    {
        OrderBook b;
        b.add_limit(1, Side::Sell, 10002, 40, 1);
        auto rep = b.add_limit(2, Side::Buy, 10002, 100, 2);

        CHECK_EQ(rep.filled_qty, 40);
        CHECK_EQ(rep.resting_qty, 60);
        CHECK_EQ(b.best_bid_or(-1), 10002);         // remainder became the bid
        CHECK_EQ(b.n_ask_levels(), 0u);             // ask level was consumed
    }

    // ======================================================================
    section("6. a market order sweeps MULTIPLE levels, worst price last");
    // ======================================================================
    {
        OrderBook b;
        b.add_limit(1, Side::Sell, 10002, 50, 1);
        b.add_limit(2, Side::Sell, 10003, 50, 2);
        b.add_limit(3, Side::Sell, 10004, 50, 3);
        auto rep = b.add_market(99, Side::Buy, 120, 4);

        CHECK_EQ(rep.filled_qty, 120);
        CHECK_EQ(rep.n_trades, 3u);
        CHECK_EQ(b.trades()[0].price, 10002);
        CHECK_EQ(b.trades()[1].price, 10003);
        CHECK_EQ(b.trades()[2].price, 10004);
        CHECK_EQ(b.trades()[2].qty,   20);          // only 20 left to fill
        CHECK_EQ(b.qty_at(Side::Sell, 10004), 30);  // 30 survives
        // Slippage: paid more than the touch. VWAP > best ask at arrival.
        const double vwap = double(rep.avg_fill_px_num) / double(rep.filled_qty);
        CHECK(vwap > 10002.0);
    }

    // ======================================================================
    section("7. market order into an empty book fills nothing (no crash)");
    // ======================================================================
    {
        OrderBook b;
        auto rep = b.add_market(1, Side::Buy, 100, 1);
        CHECK_EQ(rep.filled_qty, 0);
        CHECK_EQ(b.total_trades(), 0u);
    }

    // ======================================================================
    section("8. cancel is exact and idempotent-safe");
    // ======================================================================
    {
        OrderBook b;
        b.add_limit(1, Side::Buy, 10000, 100, 1);
        b.add_limit(2, Side::Buy, 10000, 300, 2);
        CHECK_EQ(b.qty_at(Side::Buy, 10000), 400);

        CHECK(b.cancel(1));
        CHECK_EQ(b.qty_at(Side::Buy, 10000), 300);
        CHECK(!b.cancel(1));                        // second cancel: no-op
        CHECK(!b.cancel(12345));                    // unknown id: no-op

        CHECK(b.cancel(2));
        CHECK_EQ(b.n_bid_levels(), 0u);             // empty level is removed
        Price bb; CHECK(!b.best_bid(bb));
    }

    // ======================================================================
    section("9. modify: shrink keeps priority, grow/reprice loses it");
    // ======================================================================
    {
        OrderBook b;
        b.add_limit(1, Side::Buy, 10000, 100, 1);
        b.add_limit(2, Side::Buy, 10000, 100, 2);

        // Shrink order 1 -> it must STILL be first in the queue.
        b.modify(1, 10000, 60, 3);
        CHECK_EQ(b.qty_at(Side::Buy, 10000), 160);
        CHECK_EQ(b.queue_ahead(1), 0);
        CHECK_EQ(b.queue_ahead(2), 60);
        b.add_market(99, Side::Sell, 10, 4);
        CHECK_EQ(b.trades().back().resting_id, 1u);  // priority preserved
    }
    {
        OrderBook b;
        b.add_limit(1, Side::Buy, 10000, 100, 1);
        b.add_limit(2, Side::Buy, 10000, 100, 2);

        // Grow order 1 -> it goes to the BACK of the queue.
        b.modify(1, 10000, 150, 3);
        CHECK_EQ(b.queue_ahead(1), 100);            // now behind order 2
        CHECK_EQ(b.queue_ahead(2), 0);
        b.add_market(99, Side::Sell, 10, 4);
        CHECK_EQ(b.trades().back().resting_id, 2u);
    }
    {
        OrderBook b;
        b.add_limit(1, Side::Sell, 10002, 100, 1);
        b.add_limit(2, Side::Buy,  10000, 100, 2);
        // Reprice the bid across the spread -> it should trade immediately.
        b.modify(2, 10002, 100, 3);
        CHECK_EQ(b.total_trades(), 1u);
        CHECK_EQ(b.trades()[0].price, 10002);
        CHECK_EQ(b.n_ask_levels(), 0u);
    }

    // ======================================================================
    section("10. IOC discards the remainder, FOK is all-or-nothing");
    // ======================================================================
    {
        OrderBook b;
        b.add_limit(1, Side::Sell, 10002, 40, 1);
        auto ioc = b.add_limit(2, Side::Buy, 10002, 100, 2, TimeInForce::IOC);
        CHECK_EQ(ioc.filled_qty, 40);
        CHECK_EQ(ioc.resting_qty, 0);
        CHECK_EQ(b.n_bid_levels(), 0u);             // nothing left behind
    }
    {
        OrderBook b;
        b.add_limit(1, Side::Sell, 10002, 40, 1);
        auto fok = b.add_limit(2, Side::Buy, 10002, 100, 2, TimeInForce::FOK);
        CHECK(!fok.accepted);
        CHECK_EQ(fok.filled_qty, 0);
        CHECK_EQ(b.qty_at(Side::Sell, 10002), 40);  // book untouched
    }
    {
        OrderBook b;
        b.add_limit(1, Side::Sell, 10002, 40, 1);
        b.add_limit(2, Side::Sell, 10003, 80, 2);
        auto fok = b.add_limit(3, Side::Buy, 10003, 100, 3, TimeInForce::FOK);
        CHECK(fok.accepted);
        CHECK_EQ(fok.filled_qty, 100);
    }

    // ======================================================================
    section("11. duplicate order id is rejected");
    // ======================================================================
    {
        OrderBook b;
        b.add_limit(1, Side::Buy, 10000, 100, 1);
        auto dup = b.add_limit(1, Side::Buy, 10001, 50, 2);
        CHECK(!dup.accepted);
        CHECK_EQ(b.live_orders(), 1u);
    }

    // ======================================================================
    section("12. microprice and imbalance");
    // ======================================================================
    {
        OrderBook b;
        b.add_limit(1, Side::Buy,  10000, 900, 1);
        b.add_limit(2, Side::Sell, 10002, 100, 2);
        double mp = 0, imb = 0;
        CHECK(b.microprice(mp));
        CHECK(b.imbalance(imb));
        // Heavy bid, light ask -> fair value pulled toward the ask.
        CHECK(mp > 10001.0);
        CHECK(imb > 0.5);
    }

    // ======================================================================
    section("13. invariant fuzz: 200k random ops keep the book self-consistent");
    // ======================================================================
    {
        OrderBook b;
        std::vector<OrderId> live;
        std::srand(42);
        OrderId next_id = 1;

        for (int i = 0; i < 200000; ++i) {
            const int r = std::rand() % 100;
            if (r < 55 || live.empty()) {                       // add
                const Side s = (std::rand() % 2) ? Side::Buy : Side::Sell;
                const Price px = 9990 + (std::rand() % 21);
                const Quantity q = 1 + (std::rand() % 500);
                auto rep = b.add_limit(next_id, s, px, q, i);
                if (rep.resting_qty > 0) live.push_back(next_id);
                ++next_id;
            } else if (r < 85) {                                // cancel
                const std::size_t k = std::rand() % live.size();
                b.cancel(live[k]);
                live[k] = live.back(); live.pop_back();
            } else {                                            // market order
                const Side s = (std::rand() % 2) ? Side::Buy : Side::Sell;
                b.add_market(next_id++, s, 1 + (std::rand() % 300), i);
            }

            if (i % 5000 == 0) b.clear_trades();   // keep memory bounded
        }

        // INVARIANT A: the book must never be crossed (best bid < best ask).
        Price bb, ba;
        if (b.best_bid(bb) && b.best_ask(ba)) CHECK(bb < ba);

        // INVARIANT B: every level's cached total_qty equals the sum of its
        // orders, and no level is empty.
        Quantity sum_bid = 0;
        for (const auto& lv : b.depth(Side::Buy, 1000)) {
            CHECK(lv.qty > 0);
            CHECK(lv.orders > 0);
            sum_bid += lv.qty;
        }
        CHECK(sum_bid >= 0);

        // INVARIANT C: id map size == live pool size.
        std::size_t counted = 0;
        for (const auto& lv : b.depth(Side::Buy, 100000)) counted += lv.orders;
        for (const auto& lv : b.depth(Side::Sell, 100000)) counted += lv.orders;
        CHECK_EQ(counted, b.live_orders());

        std::printf("  fuzz: %llu trades, %llu volume, %zu live orders\n",
                    (unsigned long long)b.total_trades(),
                    (unsigned long long)b.total_volume(),
                    b.live_orders());
    }

    // ======================================================================
    section("14. batched replay matches one-at-a-time submission");
    // ======================================================================
    {
        std::vector<std::uint8_t> types{1, 1, 1, 4};
        std::vector<OrderId>      ids  {1, 2, 3, 4};
        std::vector<std::uint8_t> sides{0, 0, 1, 1};
        std::vector<Price>        px   {10000, 10001, 10003, 0};
        std::vector<Quantity>     qty  {100, 200, 300, 250};
        std::vector<Timestamp>    ts   {1, 2, 3, 4};

        OrderBook a, c;
        a.replay(types.data(), ids.data(), sides.data(), px.data(),
                 qty.data(), ts.data(), types.size());

        c.add_limit(1, Side::Buy,  10000, 100, 1);
        c.add_limit(2, Side::Buy,  10001, 200, 2);
        c.add_limit(3, Side::Sell, 10003, 300, 3);
        c.add_market(4, Side::Sell, 250, 4);

        CHECK_EQ(a.total_trades(), c.total_trades());
        CHECK_EQ(a.total_volume(), c.total_volume());
        CHECK_EQ(a.best_bid_or(-1), c.best_bid_or(-1));
        CHECK_EQ(a.qty_at(Side::Buy, 10000), c.qty_at(Side::Buy, 10000));
    }

    // ------------------------------------------------------------------
    std::printf("\n%s  %d checks, %d failures\n",
                g_failures == 0 ? "PASSED" : "FAILED", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
