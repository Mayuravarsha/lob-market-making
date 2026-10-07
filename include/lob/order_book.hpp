// order_book.hpp -- a price-time-priority limit order book.
//
// ============================================================================
// WHAT A LIMIT ORDER BOOK IS
// ============================================================================
// An exchange does not "have prices". It has a book of unfilled orders:
//
//        BIDS (people willing to buy)   |   ASKS (people willing to sell)
//        price   size   #orders         |   price   size   #orders
//        10.02    900       3           |   10.03   400       2     <- best ask
//        10.01   1500       5           |   10.04  1100       4
//        10.00   2200       9           |   10.05   700       3
//          ^ best bid
//
// The best bid (highest price anyone will pay) and best ask (lowest price
// anyone will sell at) are the "top of book". The gap between them is the
// SPREAD. Nothing trades until someone crosses that gap.
//
// ============================================================================
// PRICE-TIME PRIORITY: the matching rule
// ============================================================================
// When a sell order arrives, which buyer gets filled first?
//   1. PRICE first  -- the buyer offering the highest price wins.
//   2. TIME second  -- among buyers at the same price, whoever queued first wins.
//
// So the book is: a sorted container of prices, and at each price a FIFO queue.
// That is exactly the data structure below:
//
//     std::map<Price, PriceLevel>   -> sorted prices, O(log L) lookup
//     PriceLevel = intrusive list   -> FIFO, O(1) push/pop/unlink
//     unordered_map<OrderId, Index> -> O(1) "where is order 12345?"
//
// The third map is the trick that makes CANCEL fast. Without it, cancelling
// order 12345 means searching every level -- O(N). With it, cancel is a hash
// lookup plus an O(1) unlink, which matters enormously because in real markets
// well over 90% of orders are cancelled, not filled.
//
// ============================================================================
// COMPLEXITY SUMMARY  (L = distinct price levels, not orders)
// ============================================================================
//   add limit, no match   O(log L)     -- find/create the level, push_back
//   add limit, matching   O(log L + k) -- k = number of resting orders consumed
//   cancel                O(1) expected-- hash lookup + unlink
//   modify (size down)    O(1) expected-- in place, KEEPS queue priority
//   modify (price/size up)O(log L)     -- cancel + re-add, LOSES queue priority
//   best bid / best ask   O(1)         -- map::begin()
//   depth(n)              O(n)
#pragma once

#include <cstddef>
#include <map>
#include <unordered_map>
#include <vector>

#include "lob/order.hpp"
#include "lob/price_level.hpp"
#include "lob/types.hpp"

namespace lob {

class OrderBook {
public:
    // Bids are sorted DESCENDING so bids_.begin() is the best (highest) bid.
    // Asks are sorted ASCENDING  so asks_.begin() is the best (lowest)  ask.
    using BidMap = std::map<Price, PriceLevel, std::greater<Price>>;
    using AskMap = std::map<Price, PriceLevel, std::less<Price>>;

    explicit OrderBook(std::size_t pool_reserve = 1u << 16);

    // ---------------------------------------------------------------- writes
    // Submit a limit order. It matches against the opposite side as long as it
    // is marketable, then rests whatever is left (subject to time-in-force).
    ExecReport add_limit(OrderId id, Side side, Price price, Quantity qty,
                         Timestamp ts, TimeInForce tif = TimeInForce::GTC);

    // Submit a market order: no price limit, sweep until filled or book empty.
    // Equivalent to an IOC limit at an infinitely aggressive price.
    ExecReport add_market(OrderId id, Side side, Quantity qty, Timestamp ts);

    // Remove a resting order. Returns false if the id is unknown (already
    // filled or already cancelled) -- that is normal, not an error.
    bool cancel(OrderId id);

    // Amend a resting order.
    //   * size DOWN at the same price -> done in place, queue position KEPT.
    //   * size UP, or any price change -> cancel + re-add, queue position LOST.
    // These are the real rules on most exchanges, and the asymmetry is a
    // favourite interview question: increasing your size would let you jump
    // ahead of people who queued behind you, so exchanges forbid it.
    bool modify(OrderId id, Price new_price, Quantity new_qty, Timestamp ts);

    void clear();

    // Touch every reserved page so that the first million operations do not pay
    // soft page faults. Call once after construction, before the hot loop.
    // See OrderPool::prefault for the full explanation.
    void prefault();

    // ----------------------------------------------------------------- reads
    bool  best_bid(Price& out) const;
    bool  best_ask(Price& out) const;
    Price best_bid_or(Price fallback) const;
    Price best_ask_or(Price fallback) const;

    // Mid price in ticks*2 (so it stays an integer even for odd spreads).
    // Returns false if either side is empty.
    bool mid_x2(Price& out) const;
    bool spread(Price& out) const;

    // Size-weighted mid: (bid_px*ask_sz + ask_px*bid_sz) / (bid_sz + ask_sz).
    // This is the "microprice". Intuition: if there are 5000 buyers at the bid
    // and only 100 sellers at the ask, the next trade is far more likely to
    // happen at the ask, so fair value sits closer to the ask. The microprice
    // is a better short-horizon predictor of the future mid than the mid is.
    bool microprice(double& out) const;

    // Order-book imbalance in [-1, 1]: (bid_sz - ask_sz) / (bid_sz + ask_sz).
    // Positive = buying pressure. One of the most-used features in HFT.
    bool imbalance(double& out) const;

    Quantity qty_at(Side side, Price price) const;
    std::vector<LevelInfo> depth(Side side, std::size_t n_levels) const;

    // How much volume must trade at this order's price before it fills.
    // Returns -1 if the order is not in the book.
    Quantity queue_ahead(OrderId id) const;

    bool contains(OrderId id) const { return id_to_idx_.count(id) != 0; }
    std::size_t live_orders() const { return pool_.live(); }
    std::size_t n_bid_levels() const { return bids_.size(); }
    std::size_t n_ask_levels() const { return asks_.size(); }

    // ---------------------------------------------------------------- trades
    const std::vector<Trade>& trades() const { return trades_; }
    void clear_trades() { trades_.clear(); }
    void reserve_trades(std::size_t n) { trades_.reserve(n); }
    std::size_t total_trades() const { return total_trades_; }
    Quantity total_volume() const { return total_volume_; }

    // ---------------------------------------------------------------- replay
    // Batched event replay. `types/ids/sides/prices/qtys/ts` are parallel
    // arrays of length n. This exists so that Python can push a million events
    // through the book with ONE function call instead of a million, which is
    // the difference between ~50 ns and ~1 us per event.
    std::size_t replay(const std::uint8_t* types, const OrderId* ids,
                       const std::uint8_t* sides, const Price* prices,
                       const Quantity* qtys, const Timestamp* ts,
                       std::size_t n);

private:
    template <typename MapT>
    Quantity match_against(MapT& opposite, Side aggressor_side, OrderId agg_id,
                           Price limit_px, bool has_limit, Quantity qty,
                           Timestamp ts, ExecReport& rep);

    // Is `px` acceptable to an aggressor on `side` given the resting price?
    static bool crosses(Side aggressor_side, Price resting_px, Price limit_px) {
        return aggressor_side == Side::Buy ? (resting_px <= limit_px)
                                           : (resting_px >= limit_px);
    }

    Quantity available_against(Side side, Price limit_px, bool has_limit) const;

    void insert_resting(OrderId id, Side side, Price price, Quantity qty,
                        Timestamp ts);

    OrderPool pool_;
    BidMap    bids_;
    AskMap    asks_;
    std::unordered_map<OrderId, Index> id_to_idx_;
    std::vector<Trade> trades_;
    std::size_t total_trades_ = 0;
    Quantity    total_volume_ = 0;
};

}  // namespace lob
