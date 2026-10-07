"""lobkit -- limit order book toolkit.

Import the fast C++ book if it has been built, otherwise fall back to the pure
Python reference implementation so that everything still runs.

    from lobkit import OrderBook, BUY, SELL, BACKEND
    print(BACKEND)   # 'cpp' or 'python'
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

BUY = 0
SELL = 1

try:
    import _lob  # type: ignore

    BACKEND = "cpp"
    _SIDE = (_lob.Side.Buy, _lob.Side.Sell)
    _TIF = {0: _lob.TimeInForce.GTC, 1: _lob.TimeInForce.IOC, 2: _lob.TimeInForce.FOK}

    class OrderBook(_lob.OrderBook):  # type: ignore[misc]
        """C++ book with int side arguments, so both backends look identical.

        The raw C++ class wants a Side enum. Everything else in this package
        speaks plain ints (0 = buy, 1 = sell) so that strategy code does not
        have to care which backend is loaded.
        """

        def add_limit(self, order_id, side, price, qty, ts, tif=0):
            return super().add_limit(order_id, _SIDE[side], int(price), int(qty),
                                     int(ts), _TIF[tif])

        def add_market(self, order_id, side, qty, ts):
            return super().add_market(order_id, _SIDE[side], int(qty), int(ts))

        def qty_at(self, side, price):
            return super().qty_at(_SIDE[side], int(price))

        def depth(self, side, n_levels=5):
            return [(l.price, l.qty, l.orders)
                    for l in super().depth(_SIDE[side], n_levels)]

except ImportError:  # pragma: no cover - exercised when the .so is absent
    from .pybook import PyOrderBook as OrderBook  # type: ignore

    BACKEND = "python"
    _lob = None


from .simulate import FlowSimulator, SimParams              # noqa: E402
from .strategy import (InventorySkewMM, AvellanedaStoikovMM,  # noqa: E402
                       Quote, StrategyParams)
from .backtest import Backtester, BacktestResult             # noqa: E402
from .metrics import summarize_performance                   # noqa: E402

__all__ = [
    "OrderBook", "BUY", "SELL", "BACKEND",
    "FlowSimulator", "SimParams",
    "InventorySkewMM", "AvellanedaStoikovMM", "Quote", "StrategyParams",
    "Backtester", "BacktestResult", "summarize_performance",
]
