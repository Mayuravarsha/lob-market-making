// py_lob.cpp -- pybind11 bindings: make the C++ book usable from Python.
//
// WHAT PYBIND11 DOES
// ------------------
// It is a header-only C++ library that generates a Python extension module
// (_lob.cpython-311-x86_64-linux-gnu.so on Linux, _lob.pyd on Windows). From
// Python you just `import _lob` and the C++ classes look like Python classes.
// pybind11 handles reference counting, type conversion and exceptions for you.
//
// THE THING BEGINNERS GET WRONG
// -----------------------------
// Crossing the Python/C++ boundary costs roughly 100-300 ns per call. Our
// add_limit takes ~50-150 ns. So if you call add_limit() a million times from
// Python, you spend MORE time in the boundary than in the matching engine, and
// your "fast C++ book" is not fast at all.
//
// The fix is to move the LOOP into C++. `replay_batch` below takes six numpy
// arrays and processes all N events in one call. One boundary crossing instead
// of a million. We benchmark both so the effect is measurable, not folklore.
//
// py::array::c_style | py::array::forcecast means: if the caller passes a
// Python list, or a float array, or a non-contiguous slice, numpy will convert
// it for us instead of throwing.
#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>

#include "lob/order_book.hpp"

namespace py = pybind11;
using namespace lob;
using namespace pybind11::literals;  // enables the "name"_a keyword-argument syntax

template <typename T>
using arr = py::array_t<T, py::array::c_style | py::array::forcecast>;

PYBIND11_MODULE(_lob, m) {
    m.doc() = "C++17 price-time-priority limit order book";

    py::enum_<Side>(m, "Side")
        .value("Buy", Side::Buy)
        .value("Sell", Side::Sell);

    py::enum_<TimeInForce>(m, "TimeInForce")
        .value("GTC", TimeInForce::GTC)
        .value("IOC", TimeInForce::IOC)
        .value("FOK", TimeInForce::FOK);

    py::class_<Trade>(m, "Trade")
        .def_readonly("ts", &Trade::ts)
        .def_readonly("price", &Trade::price)
        .def_readonly("qty", &Trade::qty)
        .def_readonly("aggressor_id", &Trade::aggressor_id)
        .def_readonly("resting_id", &Trade::resting_id)
        .def_readonly("aggressor_side", &Trade::aggressor_side)
        .def("__repr__", [](const Trade& t) {
            return "<Trade px=" + std::to_string(t.price) +
                   " qty=" + std::to_string(t.qty) +
                   " resting=" + std::to_string(t.resting_id) + ">";
        });

    py::class_<ExecReport>(m, "ExecReport")
        .def_readonly("order_id", &ExecReport::order_id)
        .def_readonly("filled_qty", &ExecReport::filled_qty)
        .def_readonly("resting_qty", &ExecReport::resting_qty)
        .def_readonly("accepted", &ExecReport::accepted)
        .def_readonly("n_trades", &ExecReport::n_trades)
        .def_readonly("first_trade", &ExecReport::first_trade)
        .def_property_readonly("avg_fill_price", [](const ExecReport& r) {
            return r.filled_qty > 0
                ? double(r.avg_fill_px_num) / double(r.filled_qty)
                : 0.0;
        })
        .def("__repr__", [](const ExecReport& r) {
            return "<ExecReport id=" + std::to_string(r.order_id) +
                   " filled=" + std::to_string(r.filled_qty) +
                   " resting=" + std::to_string(r.resting_qty) + ">";
        });

    py::class_<LevelInfo>(m, "LevelInfo")
        .def_readonly("price", &LevelInfo::price)
        .def_readonly("qty", &LevelInfo::qty)
        .def_readonly("orders", &LevelInfo::orders)
        .def("__repr__", [](const LevelInfo& l) {
            return "(" + std::to_string(l.price) + ", " + std::to_string(l.qty) +
                   ", " + std::to_string(l.orders) + ")";
        });

    py::class_<OrderBook>(m, "OrderBook")
        .def(py::init<std::size_t>(), py::arg("pool_reserve") = (1u << 16))

        // ---- writes -------------------------------------------------------
        .def("add_limit", &OrderBook::add_limit,
             py::arg("order_id"), py::arg("side"), py::arg("price"),
             py::arg("qty"), py::arg("ts"), py::arg("tif") = TimeInForce::GTC,
             "Submit a limit order (integer price in ticks). Matches, then rests.")
        .def("add_market", &OrderBook::add_market,
             py::arg("order_id"), py::arg("side"), py::arg("qty"), py::arg("ts"))
        .def("cancel", &OrderBook::cancel, py::arg("order_id"))
        .def("modify", &OrderBook::modify,
             py::arg("order_id"), py::arg("new_price"), py::arg("new_qty"), py::arg("ts"))
        .def("clear", &OrderBook::clear)

        // ---- reads --------------------------------------------------------
        .def_property_readonly("best_bid", [](const OrderBook& b) -> py::object {
            Price p; return b.best_bid(p) ? py::cast(p) : py::none();
        })
        .def_property_readonly("best_ask", [](const OrderBook& b) -> py::object {
            Price p; return b.best_ask(p) ? py::cast(p) : py::none();
        })
        .def_property_readonly("mid", [](const OrderBook& b) -> py::object {
            Price m; return b.mid_x2(m) ? py::cast(double(m) / 2.0) : py::none();
        })
        .def_property_readonly("spread", [](const OrderBook& b) -> py::object {
            Price s; return b.spread(s) ? py::cast(s) : py::none();
        })
        .def_property_readonly("microprice", [](const OrderBook& b) -> py::object {
            double v; return b.microprice(v) ? py::cast(v) : py::none();
        })
        .def_property_readonly("imbalance", [](const OrderBook& b) -> py::object {
            double v; return b.imbalance(v) ? py::cast(v) : py::none();
        })
        .def("qty_at", &OrderBook::qty_at, py::arg("side"), py::arg("price"))
        .def("depth", &OrderBook::depth, py::arg("side"), py::arg("n_levels") = 5)
        .def("queue_ahead", &OrderBook::queue_ahead, py::arg("order_id"),
             "Total resting size ahead of this order in its FIFO queue (-1 if absent).")
        .def("contains", &OrderBook::contains, py::arg("order_id"))
        .def_property_readonly("live_orders", &OrderBook::live_orders)
        .def_property_readonly("n_bid_levels", &OrderBook::n_bid_levels)
        .def_property_readonly("n_ask_levels", &OrderBook::n_ask_levels)
        .def_property_readonly("total_trades", &OrderBook::total_trades)
        .def_property_readonly("total_volume", &OrderBook::total_volume)

        // ---- trades -------------------------------------------------------
        // NOTE: pybind11's std::vector caster COPIES. Calling trades() in a
        // hot loop therefore rebuilds a Python list of every trade ever, which
        // turns an O(1) read into O(n) and the whole backtest into O(n^2).
        // trades_since() is the one to use in a loop -- it copies only the
        // handful of trades generated since your cursor.
        .def("trades", &OrderBook::trades,
             "Copy the WHOLE trade buffer. Fine for inspection, never call it in a loop.")
        .def("trades_since", [](const OrderBook& b, std::size_t idx) {
            const auto& t = b.trades();
            if (idx >= t.size()) return std::vector<Trade>{};
            return std::vector<Trade>(t.begin() + static_cast<std::ptrdiff_t>(idx), t.end());
        }, py::arg("cursor"), "Trades appended after `cursor`. O(new trades).")
        .def("n_trades_buffered", [](const OrderBook& b) { return b.trades().size(); })
        .def("clear_trades", &OrderBook::clear_trades)
        .def("reserve_trades", &OrderBook::reserve_trades, py::arg("n"))

        // Trades as numpy columns: far cheaper than building N Python objects
        // when you want to post-process a million fills in pandas.
        .def("trades_as_arrays", [](const OrderBook& b) {
            const auto& t = b.trades();
            const std::size_t n = t.size();
            arr<Timestamp> ts(n); arr<Price> px(n); arr<Quantity> qty(n);
            arr<std::uint64_t> agg(n), rest(n); arr<std::uint8_t> side(n);
            auto *pts = ts.mutable_data(), *ppx = px.mutable_data();
            auto *pq = qty.mutable_data();
            auto *pa = agg.mutable_data(); auto *pr = rest.mutable_data();
            auto *ps = side.mutable_data();
            for (std::size_t i = 0; i < n; ++i) {
                pts[i] = t[i].ts; ppx[i] = t[i].price; pq[i] = t[i].qty;
                pa[i] = t[i].aggressor_id; pr[i] = t[i].resting_id;
                ps[i] = static_cast<std::uint8_t>(t[i].aggressor_side);
            }
            return py::dict("ts"_a = ts, "price"_a = px, "qty"_a = qty,
                            "aggressor_id"_a = agg, "resting_id"_a = rest,
                            "aggressor_side"_a = side);
        })

        // ---- batched replay: the whole point of the bindings ---------------
        .def("replay_batch", [](OrderBook& b,
                                arr<std::uint8_t> types, arr<std::uint64_t> ids,
                                arr<std::uint8_t> sides, arr<std::int64_t> prices,
                                arr<std::int64_t> qtys, arr<std::int64_t> ts) {
            const std::size_t n = static_cast<std::size_t>(types.size());
            if (ids.size() != (py::ssize_t)n || sides.size() != (py::ssize_t)n ||
                prices.size() != (py::ssize_t)n || qtys.size() != (py::ssize_t)n ||
                ts.size() != (py::ssize_t)n)
                throw std::invalid_argument("replay_batch: all arrays must be the same length");

            // Release the GIL: no Python objects are touched inside, so other
            // Python threads can run while the engine chews through the batch.
            std::size_t n_trades;
            {
                py::gil_scoped_release release;
                n_trades = b.replay(types.data(), ids.data(), sides.data(),
                                    prices.data(), qtys.data(), ts.data(), n);
            }
            return n_trades;
        }, py::arg("types"), py::arg("ids"), py::arg("sides"),
           py::arg("prices"), py::arg("qtys"), py::arg("ts"),
           "Replay N events in a single call. Returns the number of trades generated.");

    m.attr("EV_ADD_LIMIT")  = static_cast<std::uint8_t>(EventType::AddLimit);
    m.attr("EV_CANCEL")     = static_cast<std::uint8_t>(EventType::Cancel);
    m.attr("EV_MODIFY")     = static_cast<std::uint8_t>(EventType::Modify);
    m.attr("EV_ADD_MARKET") = static_cast<std::uint8_t>(EventType::AddMarket);
    m.attr("__version__")   = "0.1.0";
}
