// order.hpp -- a resting order and the pool that owns all of them.
//
// WHY A POOL?
// -----------
// The hot loop of a matching engine is add / cancel / add / cancel, millions
// of times a second. If every add called `new` and every cancel called
// `delete`, we would spend most of our time in the system allocator, which
// takes a lock and hands back memory scattered all over RAM (bad for the CPU
// cache).
//
// Instead we allocate one big std::vector<Order> up front and hand out slots
// from it. A freed slot goes on a singly-linked "free list" and is reused by
// the next add. Allocation becomes: pop the head of the free list. That is
// three instructions, no lock, and the memory stays hot in cache.
//
// WHY INDICES INSTEAD OF POINTERS?
// --------------------------------
// The pool is a std::vector, so it can grow (reallocate) and invalidate every
// pointer into it. Indices survive reallocation. They are also half the size
// of a 64-bit pointer, so the Order struct stays small.
#pragma once

#include <cstddef>
#include <vector>

#include "lob/types.hpp"

namespace lob {

// A live order resting in the book.
//
// prev/next make each price level an INTRUSIVE doubly-linked list. "Intrusive"
// just means the link fields live inside the Order itself rather than in a
// separate node object. That is what makes cancel O(1): given the order, we
// already have its neighbours, so we can unlink it without searching.
struct Order {
    OrderId  id    = 0;
    Price    price = 0;
    Quantity qty   = 0;      // remaining (unfilled) quantity
    Timestamp ts   = 0;      // time the order gained its current queue position
    Index    prev  = kNullIndex;  // order ahead of us in the FIFO queue
    Index    next  = kNullIndex;  // order behind us
    Side     side  = Side::Buy;
    bool     alive = false;
};

class OrderPool {
public:
    explicit OrderPool(std::size_t reserve = 1u << 16) {
        slots_.reserve(reserve);
    }

    // Grab a free slot. O(1) amortised.
    Index acquire() {
        if (free_head_ != kNullIndex) {
            const Index idx = free_head_;
            free_head_ = slots_[idx].next;   // reuse `next` as the free-list link
            slots_[idx] = Order{};           // reset to a clean state
            slots_[idx].alive = true;
            ++live_;
            return idx;
        }
        slots_.push_back(Order{});
        slots_.back().alive = true;
        ++live_;
        return static_cast<Index>(slots_.size() - 1);
    }

    // Return a slot to the free list. O(1).
    void release(Index idx) {
        slots_[idx].alive = false;
        slots_[idx].next  = free_head_;
        free_head_ = idx;
        --live_;
    }

    Order&       operator[](Index i)       { return slots_[i]; }
    const Order& operator[](Index i) const { return slots_[i]; }

    std::size_t live() const noexcept     { return live_; }
    std::size_t capacity() const noexcept { return slots_.capacity(); }

    void clear() {
        slots_.clear();
        free_head_ = kNullIndex;
        live_ = 0;
    }

    // PRE-FAULT the pool.
    //
    // reserve() only asks the OS for a virtual address range. The physical
    // pages are not attached until the first time each 4 KiB page is written,
    // and each of those first touches is a "soft page fault" -- a trap into the
    // kernel costing roughly 1-2 microseconds. Spread across a run they show up
    // as a fat p99 tail even though the median is 30 ns.
    //
    // Touching every page up front moves all of that cost into startup, where
    // nobody cares. This is standard practice in low-latency systems, usually
    // alongside mlock() and huge pages.
    void prefault() {
        const std::size_t cap = slots_.capacity();
        const std::size_t n   = slots_.size();
        if (cap <= n) return;
        slots_.resize(cap);                 // writes to every page => faults now
        for (std::size_t i = cap; i-- > n;) {   // hand the new slots to the free list
            slots_[i].alive = false;
            slots_[i].next  = free_head_;
            free_head_      = static_cast<Index>(i);
        }
    }

private:
    std::vector<Order> slots_;
    Index       free_head_ = kNullIndex;
    std::size_t live_      = 0;
};

}  // namespace lob
