// price_level.hpp -- all the orders sitting at one price, in arrival order.
//
// A limit order book is a collection of price levels. Each level is a FIFO
// queue: the order that arrived first is at the head and will be filled first.
// That is the "time" half of "price-time priority".
//
// We cache total_qty so that "how much size is at $10.02?" is O(1) instead of
// a walk down the list. Every real book does this; it is what feeds the
// Level-2 depth display you see on a trading screen.
#pragma once

#include "lob/order.hpp"
#include "lob/types.hpp"

namespace lob {

class PriceLevel {
public:
    // Append to the back of the queue (worst priority). This is what happens
    // when a new limit order rests.
    void push_back(OrderPool& pool, Index idx) {
        Order& o = pool[idx];
        o.prev = tail_;
        o.next = kNullIndex;
        if (tail_ != kNullIndex) pool[tail_].next = idx;
        else                     head_ = idx;
        tail_ = idx;
        total_qty_ += o.qty;
        ++count_;
    }

    // Unlink an arbitrary order. O(1) because the order carries its own links.
    void unlink(OrderPool& pool, Index idx) {
        Order& o = pool[idx];
        if (o.prev != kNullIndex) pool[o.prev].next = o.next;
        else                      head_ = o.next;
        if (o.next != kNullIndex) pool[o.next].prev = o.prev;
        else                      tail_ = o.prev;
        o.prev = o.next = kNullIndex;
        total_qty_ -= o.qty;
        --count_;
    }

    // Reduce an order's size in place, keeping its queue position.
    void reduce_qty(Quantity delta) { total_qty_ -= delta; }

    Index head() const noexcept { return head_; }
    Index tail() const noexcept { return tail_; }
    Quantity total_qty() const noexcept { return total_qty_; }
    std::uint32_t count() const noexcept { return count_; }
    bool empty() const noexcept { return head_ == kNullIndex; }

    // Total size resting AHEAD of `idx` in this queue. This is the single most
    // important number for a market maker: it is how much volume has to trade
    // at this price before *your* order gets filled.
    Quantity qty_ahead(const OrderPool& pool, Index idx) const {
        Quantity ahead = 0;
        for (Index i = head_; i != kNullIndex && i != idx; i = pool[i].next)
            ahead += pool[i].qty;
        return ahead;
    }

private:
    Index head_ = kNullIndex;
    Index tail_ = kNullIndex;
    Quantity total_qty_ = 0;
    std::uint32_t count_ = 0;
};

}  // namespace lob
