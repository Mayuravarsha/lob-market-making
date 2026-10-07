// types.hpp -- the vocabulary of the order book.
//
// Everything in this project is built out of the handful of types below.
// Two deliberate choices worth knowing about:
//
//  1) Prices are INTEGERS, not doubles. Exchanges quote on a discrete grid
//     ("ticks"). If AAPL trades in $0.01 increments, we store $150.23 as the
//     integer 15023. Integer comparison is exact and fast; floating point
//     comparison is neither (0.1 + 0.2 != 0.3). Every real matching engine
//     does this.
//
//  2) Timestamps are int64 nanoseconds since some epoch. Market data feeds
//     are nanosecond stamped and a double loses precision past ~2^53 ns.
#pragma once

#include <cstdint>
#include <limits>

namespace lob {

using OrderId   = std::uint64_t;  // unique per order, assigned by us or the feed
using Price     = std::int64_t;   // in integer ticks
using Quantity  = std::int64_t;   // in shares / lots / contracts
using Timestamp = std::int64_t;   // nanoseconds

// Index into the order pool. 32 bits is plenty (4 billion live orders) and
// halves the memory of the linked-list pointers versus a 64-bit pointer.
using Index = std::uint32_t;
inline constexpr Index kNullIndex = std::numeric_limits<Index>::max();

enum class Side : std::uint8_t { Buy = 0, Sell = 1 };

inline constexpr Side opposite(Side s) noexcept {
    return s == Side::Buy ? Side::Sell : Side::Buy;
}

// What happens to the part of a limit order that cannot be filled immediately.
enum class TimeInForce : std::uint8_t {
    GTC,  // Good-Til-Cancelled: rest the remainder on the book (the default)
    IOC,  // Immediate-Or-Cancel: fill what you can, discard the rest
    FOK   // Fill-Or-Kill: fill the WHOLE order immediately or do nothing at all
};

// A single execution. Two orders always participate: the "aggressor" (the
// incoming order that crossed the spread) and the "resting" order (the one
// that was already sitting on the book, providing liquidity).
//
// The trade always prints at the RESTING order's price. This is the price
// improvement rule: if you send a buy limit at $10.05 and the best offer is
// $10.02, you pay $10.02, not $10.05.
struct Trade {
    Timestamp ts        = 0;
    Price     price     = 0;
    Quantity  qty       = 0;
    OrderId   aggressor_id = 0;
    OrderId   resting_id   = 0;
    Side      aggressor_side = Side::Buy;  // Buy => the aggressor lifted an offer
};

// Result of submitting an order.
struct ExecReport {
    OrderId  order_id      = 0;
    Quantity filled_qty    = 0;   // how much traded immediately
    Quantity resting_qty   = 0;   // how much is now sitting on the book
    Price    avg_fill_px_num = 0; // sum(price*qty), divide by filled_qty for VWAP
    bool     accepted      = false;
    std::size_t first_trade = 0;  // index into OrderBook::trades() of our first trade
    std::size_t n_trades    = 0;  // how many trades this order generated
};

// One level of a Level-2 (aggregated depth) snapshot.
struct LevelInfo {
    Price     price = 0;
    Quantity  qty   = 0;   // total resting size at this price
    std::uint32_t orders = 0;  // how many distinct orders make up that size
};

// Event types for the batched replay interface. Keeping this as a small int
// enum lets us feed the book from a flat numpy array with zero per-event
// Python overhead.
enum class EventType : std::uint8_t {
    AddLimit  = 1,
    Cancel    = 2,
    Modify    = 3,
    AddMarket = 4
};

}  // namespace lob
