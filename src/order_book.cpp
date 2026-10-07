#include "lob/order_book.hpp"

#include <algorithm>

namespace lob {

OrderBook::OrderBook(std::size_t pool_reserve) : pool_(pool_reserve) {
    trades_.reserve(1u << 12);
    // Pre-size the id -> slot hash map. Without this the map rehashes ~log2(N)
    // times as it grows, and each rehash re-inserts every element -- a visible
    // latency spike right in the middle of the hot path.
    id_to_idx_.reserve(pool_reserve);
}

void OrderBook::prefault() {
    pool_.prefault();
    const std::size_t cap = trades_.capacity();
    trades_.resize(cap);   // touch every page of the trade buffer
    trades_.clear();       // clear() keeps the capacity, so the pages stay ours
}

void OrderBook::clear() {
    pool_.clear();
    bids_.clear();
    asks_.clear();
    id_to_idx_.clear();
    trades_.clear();
    total_trades_ = 0;
    total_volume_ = 0;
}

// ---------------------------------------------------------------------------
// Matching.
//
// We repeatedly look at the BEST level on the opposite side. If the aggressor
// is willing to trade there, we consume orders from the head of that level's
// FIFO queue until either the aggressor is filled or the level is exhausted.
// Empty levels are erased so that `begin()` is always the true top of book.
// ---------------------------------------------------------------------------
template <typename MapT>
Quantity OrderBook::match_against(MapT& opposite, Side aggressor_side,
                                  OrderId agg_id, Price limit_px, bool has_limit,
                                  Quantity qty, Timestamp ts, ExecReport& rep) {
    Quantity filled = 0;

    while (qty > 0 && !opposite.empty()) {
        auto it = opposite.begin();          // best price on the far side
        const Price px = it->first;

        // Stop as soon as the far side is more expensive than we are willing
        // to pay (or cheaper than we are willing to sell at).
        if (has_limit && !crosses(aggressor_side, px, limit_px)) break;

        PriceLevel& lvl = it->second;

        while (qty > 0 && !lvl.empty()) {
            const Index hidx = lvl.head();   // oldest order => highest priority
            Order& resting = pool_[hidx];
            const Quantity take = std::min(qty, resting.qty);

            // The trade prints at the RESTING order's price (price improvement).
            trades_.push_back(Trade{ts, px, take, agg_id, resting.id, aggressor_side});
            rep.avg_fill_px_num += px * take;

            qty    -= take;
            filled += take;
            total_volume_ += take;
            ++total_trades_;

            if (take == resting.qty) {
                const OrderId rid = resting.id;
                lvl.unlink(pool_, hidx);     // fully filled -> leaves the book
                id_to_idx_.erase(rid);
                pool_.release(hidx);
            } else {
                resting.qty -= take;         // partial fill -> keeps its place
                lvl.reduce_qty(take);
            }
        }

        if (lvl.empty()) opposite.erase(it);
    }
    return filled;
}

Quantity OrderBook::available_against(Side side, Price limit_px, bool has_limit) const {
    Quantity avail = 0;
    if (side == Side::Buy) {
        for (const auto& [px, lvl] : asks_) {
            if (has_limit && !crosses(Side::Buy, px, limit_px)) break;
            avail += lvl.total_qty();
        }
    } else {
        for (const auto& [px, lvl] : bids_) {
            if (has_limit && !crosses(Side::Sell, px, limit_px)) break;
            avail += lvl.total_qty();
        }
    }
    return avail;
}

void OrderBook::insert_resting(OrderId id, Side side, Price price, Quantity qty,
                               Timestamp ts) {
    const Index idx = pool_.acquire();
    Order& o = pool_[idx];
    o.id = id; o.price = price; o.qty = qty; o.ts = ts; o.side = side;

    if (side == Side::Buy) bids_[price].push_back(pool_, idx);
    else                   asks_[price].push_back(pool_, idx);

    id_to_idx_[id] = idx;
}

ExecReport OrderBook::add_limit(OrderId id, Side side, Price price, Quantity qty,
                                Timestamp ts, TimeInForce tif) {
    ExecReport rep;
    rep.order_id    = id;
    rep.first_trade = trades_.size();

    if (qty <= 0 || id_to_idx_.count(id)) return rep;  // rejected: dup id / bad size

    // Fill-or-Kill: check we can complete the whole thing BEFORE touching the
    // book, otherwise we would have to unwind partial fills.
    if (tif == TimeInForce::FOK && available_against(side, price, true) < qty) {
        return rep;  // killed
    }
    rep.accepted = true;

    Quantity filled = (side == Side::Buy)
        ? match_against(asks_, Side::Buy,  id, price, true, qty, ts, rep)
        : match_against(bids_, Side::Sell, id, price, true, qty, ts, rep);

    rep.filled_qty = filled;
    rep.n_trades   = trades_.size() - rep.first_trade;

    const Quantity remaining = qty - filled;
    if (remaining > 0 && tif == TimeInForce::GTC) {
        insert_resting(id, side, price, remaining, ts);
        rep.resting_qty = remaining;
    }
    return rep;
}

ExecReport OrderBook::add_market(OrderId id, Side side, Quantity qty, Timestamp ts) {
    ExecReport rep;
    rep.order_id    = id;
    rep.first_trade = trades_.size();
    if (qty <= 0) return rep;
    rep.accepted = true;

    // has_limit = false => sweep the book at any price.
    Quantity filled = (side == Side::Buy)
        ? match_against(asks_, Side::Buy,  id, 0, false, qty, ts, rep)
        : match_against(bids_, Side::Sell, id, 0, false, qty, ts, rep);

    rep.filled_qty = filled;
    rep.n_trades   = trades_.size() - rep.first_trade;
    // Any unfilled remainder of a market order is simply discarded.
    return rep;
}

bool OrderBook::cancel(OrderId id) {
    auto it = id_to_idx_.find(id);
    if (it == id_to_idx_.end()) return false;   // unknown id: normal, not an error

    const Index idx = it->second;
    const Order& o  = pool_[idx];

    if (o.side == Side::Buy) {
        auto lit = bids_.find(o.price);
        lit->second.unlink(pool_, idx);
        if (lit->second.empty()) bids_.erase(lit);
    } else {
        auto lit = asks_.find(o.price);
        lit->second.unlink(pool_, idx);
        if (lit->second.empty()) asks_.erase(lit);
    }

    id_to_idx_.erase(it);
    pool_.release(idx);
    return true;
}

bool OrderBook::modify(OrderId id, Price new_price, Quantity new_qty, Timestamp ts) {
    auto it = id_to_idx_.find(id);
    if (it == id_to_idx_.end()) return false;

    const Index idx = it->second;
    const Order  old = pool_[idx];   // copy: we may destroy the slot below

    if (new_qty <= 0) return cancel(id);
    if (new_price == old.price && new_qty == old.qty) return true;   // no-op

    // ---- Case 1: shrink at the same price. Priority is preserved. ----------
    if (new_price == old.price && new_qty < old.qty) {
        const Quantity delta = old.qty - new_qty;
        pool_[idx].qty = new_qty;
        if (old.side == Side::Buy) bids_.find(old.price)->second.reduce_qty(delta);
        else                       asks_.find(old.price)->second.reduce_qty(delta);
        return true;
    }

    // ---- Case 2: price change or size increase. Priority is LOST. ----------
    // Implemented as cancel + re-add, which also means the amended order can
    // now cross the spread and trade immediately.
    cancel(id);
    add_limit(id, old.side, new_price, new_qty, ts, TimeInForce::GTC);
    return true;
}

// --------------------------------------------------------------------- reads

bool OrderBook::best_bid(Price& out) const {
    if (bids_.empty()) return false;
    out = bids_.begin()->first;
    return true;
}
bool OrderBook::best_ask(Price& out) const {
    if (asks_.empty()) return false;
    out = asks_.begin()->first;
    return true;
}
Price OrderBook::best_bid_or(Price fallback) const {
    return bids_.empty() ? fallback : bids_.begin()->first;
}
Price OrderBook::best_ask_or(Price fallback) const {
    return asks_.empty() ? fallback : asks_.begin()->first;
}

bool OrderBook::mid_x2(Price& out) const {
    if (bids_.empty() || asks_.empty()) return false;
    out = bids_.begin()->first + asks_.begin()->first;
    return true;
}

bool OrderBook::spread(Price& out) const {
    if (bids_.empty() || asks_.empty()) return false;
    out = asks_.begin()->first - bids_.begin()->first;
    return true;
}

bool OrderBook::microprice(double& out) const {
    if (bids_.empty() || asks_.empty()) return false;
    const auto& b = *bids_.begin();
    const auto& a = *asks_.begin();
    const double bq = static_cast<double>(b.second.total_qty());
    const double aq = static_cast<double>(a.second.total_qty());
    if (bq + aq <= 0.0) return false;
    // Weight each price by the size on the OPPOSITE side: a big bid queue
    // pushes fair value up toward the ask.
    out = (static_cast<double>(b.first) * aq + static_cast<double>(a.first) * bq) / (aq + bq);
    return true;
}

bool OrderBook::imbalance(double& out) const {
    if (bids_.empty() || asks_.empty()) return false;
    const double bq = static_cast<double>(bids_.begin()->second.total_qty());
    const double aq = static_cast<double>(asks_.begin()->second.total_qty());
    if (bq + aq <= 0.0) return false;
    out = (bq - aq) / (bq + aq);
    return true;
}

Quantity OrderBook::qty_at(Side side, Price price) const {
    if (side == Side::Buy) {
        auto it = bids_.find(price);
        return it == bids_.end() ? 0 : it->second.total_qty();
    }
    auto it = asks_.find(price);
    return it == asks_.end() ? 0 : it->second.total_qty();
}

std::vector<LevelInfo> OrderBook::depth(Side side, std::size_t n_levels) const {
    std::vector<LevelInfo> out;
    out.reserve(n_levels);
    if (side == Side::Buy) {
        for (const auto& [px, lvl] : bids_) {
            if (out.size() >= n_levels) break;
            out.push_back(LevelInfo{px, lvl.total_qty(), lvl.count()});
        }
    } else {
        for (const auto& [px, lvl] : asks_) {
            if (out.size() >= n_levels) break;
            out.push_back(LevelInfo{px, lvl.total_qty(), lvl.count()});
        }
    }
    return out;
}

Quantity OrderBook::queue_ahead(OrderId id) const {
    auto it = id_to_idx_.find(id);
    if (it == id_to_idx_.end()) return -1;
    const Index idx = it->second;
    const Order& o  = pool_[idx];
    if (o.side == Side::Buy) {
        auto lit = bids_.find(o.price);
        return lit == bids_.end() ? -1 : lit->second.qty_ahead(pool_, idx);
    }
    auto lit = asks_.find(o.price);
    return lit == asks_.end() ? -1 : lit->second.qty_ahead(pool_, idx);
}

// -------------------------------------------------------------------- replay

std::size_t OrderBook::replay(const std::uint8_t* types, const OrderId* ids,
                              const std::uint8_t* sides, const Price* prices,
                              const Quantity* qtys, const Timestamp* ts,
                              std::size_t n) {
    const std::size_t before = total_trades_;
    for (std::size_t i = 0; i < n; ++i) {
        const auto side = static_cast<Side>(sides[i]);
        switch (static_cast<EventType>(types[i])) {
            case EventType::AddLimit:
                add_limit(ids[i], side, prices[i], qtys[i], ts[i]); break;
            case EventType::Cancel:
                cancel(ids[i]); break;
            case EventType::Modify:
                modify(ids[i], prices[i], qtys[i], ts[i]); break;
            case EventType::AddMarket:
                add_market(ids[i], side, qtys[i], ts[i]); break;
        }
    }
    return total_trades_ - before;
}

// Explicit instantiation so the template lives in this translation unit.
template Quantity OrderBook::match_against<OrderBook::BidMap>(
    OrderBook::BidMap&, Side, OrderId, Price, bool, Quantity, Timestamp, ExecReport&);
template Quantity OrderBook::match_against<OrderBook::AskMap>(
    OrderBook::AskMap&, Side, OrderId, Price, bool, Quantity, Timestamp, ExecReport&);

}  // namespace lob
