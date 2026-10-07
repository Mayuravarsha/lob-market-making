"""Performance metrics -- and what each one actually tells you.

The whole point of this file is that a single headline number is never enough.
A market-making strategy can have a great Sharpe and still be a disaster (it
was never really filled, or it is quietly accumulating a huge position), so we
compute a small panel of numbers that together are hard to fake.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np

BUY, SELL = 0, 1
SECONDS_PER_TRADING_YEAR = 6.5 * 3600 * 252   # 6.5h/day, 252 days


# ---------------------------------------------------------------------------
def resample_to_grid(t: np.ndarray, y: np.ndarray, dt: float = 1.0):
    """Sample an irregular time series onto an evenly spaced grid.

    WHY THIS MATTERS FOR SHARPE
    ---------------------------
    Our events arrive at random times. If you compute returns between
    consecutive raw samples, each return covers a different amount of elapsed
    time, so their standard deviation is meaningless -- and Sharpe is
    mean/stdev. Putting everything on a fixed 1-second grid first makes the
    per-interval returns comparable, which is the assumption the Sharpe formula
    is built on.

    Uses last-observation-carried-forward, which is the correct convention for
    a price or an equity level (you hold the last known value until it updates).
    """
    if len(t) == 0:
        return np.array([]), np.array([])
    grid = np.arange(t[0], t[-1] + dt, dt)
    idx = np.searchsorted(t, grid, side="right") - 1
    idx = np.clip(idx, 0, len(y) - 1)
    return grid, y[idx]


def sharpe_ratio(pnl_increments: np.ndarray, dt_seconds: float) -> float:
    """Annualised Sharpe of a PnL series sampled every `dt_seconds`.

    Sharpe = mean(return) / stdev(return), scaled to a year by sqrt(n_periods).

    THE ASSUMPTIONS, STATED OUT LOUD (know these; you will be asked)
    ---------------------------------------------------------------
    * risk-free rate is taken as 0 -- fine for an intraday strategy that ends
      the day flat and holds no overnight capital.
    * returns are treated as i.i.d.; the sqrt-of-time scaling is only valid if
      they are. High-frequency PnL is usually negatively autocorrelated (mean
      reversion in inventory), which makes the naive annualised number
      OPTIMISTIC. Quote it, but do not defend it to four decimal places.
    * this is a PnL-based Sharpe, not a return-on-capital Sharpe: market
      makers hold near-zero average inventory, so "capital deployed" is not
      well defined and the ratio would be arbitrary.
    """
    x = np.asarray(pnl_increments, dtype=float)
    x = x[np.isfinite(x)]
    if x.size < 2:
        return float("nan")
    sd = x.std(ddof=1)
    if sd == 0:
        return float("nan")
    periods_per_year = SECONDS_PER_TRADING_YEAR / dt_seconds
    return float(x.mean() / sd * np.sqrt(periods_per_year))


def max_drawdown(equity: np.ndarray) -> Dict[str, float]:
    """Largest peak-to-trough fall in the equity curve.

    Computed on DOLLARS, not percent: a market maker's equity curve starts at
    zero, so a percentage drawdown would be a division by zero.
    """
    e = np.asarray(equity, dtype=float)
    if e.size == 0:
        return {"max_drawdown": float("nan"), "peak_idx": -1, "trough_idx": -1}
    running_peak = np.maximum.accumulate(e)
    dd = e - running_peak
    trough = int(np.argmin(dd))
    peak = int(np.argmax(e[:trough + 1])) if trough > 0 else 0
    return {"max_drawdown": float(-dd[trough]),
            "peak_idx": peak, "trough_idx": trough}


# ---------------------------------------------------------------------------
def markouts(fills: List[dict], t_grid: np.ndarray, mid_grid: np.ndarray,
             horizons: Sequence[float] = (1.0, 5.0, 30.0),
             tick_size: float = 0.01) -> Dict[str, float]:
    """Adverse selection, measured properly.

    For every fill, look at where the mid price was `h` seconds LATER:

        buy  markout = (mid_{t+h} - fill_price) * qty
        sell markout = (fill_price - mid_{t+h}) * qty

    Positive means the market moved in your favour after you traded: you bought
    and it went up. Negative means you were picked off.

    HOW TO READ THE RESULT
    ----------------------
    A healthy market maker has markouts that are slightly NEGATIVE at short
    horizons -- you are always a little bit on the wrong side, that is the price
    of providing liquidity -- but small enough that the spread you capture more
    than covers them. The decomposition

        total PnL  ~  spread captured  +  markout  +  fees/rebates

    is the standard way desks explain where the money came from, and it is a
    much stronger thing to present than a bare Sharpe ratio.

    If markouts get MORE negative as the horizon lengthens, your quotes are
    stale: the market is moving and you are not repricing fast enough.

    THE DECOMPOSITION THAT MAKES THIS READABLE
    ------------------------------------------
    A positive markout does NOT mean there is no adverse selection. Split it:

        markout            = sign * (mid_{t+h} - fill_price)
                           = sign * (mid_t - fill_price)     <- SPREAD CAPTURE
                           + sign * (mid_{t+h} - mid_t)      <- POST-TRADE DRIFT

    Spread capture is the edge you quoted; it is positive by construction for a
    passive market maker (you bought below the mid). Post-trade drift is the
    part that is actually adverse selection: how the market moved AFTER you
    traded, with the sign of your trade. Almost always negative -- that is the
    cost of being the one who was standing there.

    You are making money when   spread capture + drift + rebate > 0.
    Reporting only the combined markout hides which of the three is doing the
    work, and that is the first thing a desk will ask you.
    """
    out: Dict[str, float] = {}
    if not fills or t_grid.size == 0:
        return {f"markout_{h}s_per_share": float("nan") for h in horizons}

    ft = np.array([f["t"] for f in fills])
    fpx = np.array([f["price"] for f in fills], dtype=float)
    fq = np.array([f["qty"] for f in fills], dtype=float)
    fmid = np.array([f["mid_at_fill"] for f in fills], dtype=float)
    fsign = np.array([1.0 if f["side"] == BUY else -1.0 for f in fills])

    total_shares = fq.sum()
    for h in horizons:
        idx = np.searchsorted(t_grid, ft + h, side="right") - 1
        valid = (idx >= 0) & (ft + h <= t_grid[-1])
        idx = np.clip(idx, 0, len(mid_grid) - 1)
        future_mid = mid_grid[idx]

        mo = np.where(valid, fsign * (future_mid - fpx) * fq * tick_size, np.nan)
        drift = np.where(valid, fsign * (future_mid - fmid) * fq * tick_size, np.nan)

        out[f"markout_{h}s_total"] = float(np.nansum(mo))
        out[f"markout_{h}s_per_share"] = (float(np.nansum(mo) / total_shares)
                                          if total_shares else float("nan"))
        # The adverse-selection component, isolated. Negative is normal.
        out[f"adverse_sel_{h}s_per_share"] = (float(np.nansum(drift) / total_shares)
                                              if total_shares else float("nan"))
        out[f"adverse_sel_{h}s_total"] = float(np.nansum(drift))
    return out


# ---------------------------------------------------------------------------
def summarize_performance(result, dt: float = 1.0,
                          horizons: Sequence[float] = (1.0, 5.0, 30.0)) -> Dict[str, float]:
    """Build the full performance panel from a BacktestResult."""
    tick = result.meta.get("tick_size", 0.01)

    grid_t, grid_eq = resample_to_grid(result.t, result.equity, dt)
    _, grid_mid = resample_to_grid(result.t, result.mid, dt)
    _, grid_inv = resample_to_grid(result.t, result.inventory, dt)

    pnl_inc = np.diff(grid_eq) if grid_eq.size > 1 else np.array([])

    fills = result.fills
    n_fills = len(fills)
    filled_shares = sum(f["qty"] for f in fills)
    buy_shares = sum(f["qty"] for f in fills if f["side"] == BUY)
    sell_shares = sum(f["qty"] for f in fills if f["side"] == SELL)
    total_fees = sum(f["fee"] for f in fills)

    # Spread capture: for each fill, how far from the mid did we transact?
    # Buying below the mid or selling above it is edge; this is the gross
    # revenue side of the PnL decomposition.
    if n_fills:
        cap = np.array([
            (f["mid_at_fill"] - f["price"]) * (1 if f["side"] == BUY else -1) * f["qty"]
            for f in fills], dtype=float) * tick
        spread_capture = float(np.nansum(cap))
        spread_capture_per_share = float(np.nansum(cap) / filled_shares)
    else:
        spread_capture = spread_capture_per_share = float("nan")

    dd = max_drawdown(grid_eq)
    hours = result.sim_seconds / 3600.0 if result.sim_seconds else float("nan")

    stats: Dict[str, float] = {
        # --- headline -------------------------------------------------------
        "final_pnl": float(grid_eq[-1]) if grid_eq.size else float("nan"),
        "pnl_per_hour": float(grid_eq[-1] / hours) if grid_eq.size and hours else float("nan"),
        "sharpe_annualised": sharpe_ratio(pnl_inc, dt),
        "max_drawdown": dd["max_drawdown"],
        "return_over_maxdd": (float(grid_eq[-1]) / dd["max_drawdown"]
                              if dd["max_drawdown"] > 0 else float("nan")),

        # --- activity -------------------------------------------------------
        "n_market_events": float(result.n_events),
        "sim_hours": hours,
        "n_fills": float(n_fills),
        "filled_shares": float(filled_shares),
        "quotes_placed": float(result.n_quotes_placed),
        "quotes_cancelled": float(result.n_quotes_cancelled),
        # Fill rate = what fraction of the orders we posted ever traded.
        # Real market makers sit in the 1-10% range; anything near 50% means
        # your simulated fills are far too generous.
        "fill_rate_pct": (100.0 * n_fills / result.n_quotes_placed
                          if result.n_quotes_placed else float("nan")),
        "fills_per_hour": float(n_fills / hours) if hours else float("nan"),
        "buy_shares": float(buy_shares),
        "sell_shares": float(sell_shares),
        # Two-sidedness: a market maker should trade both ways in roughly equal
        # measure. A big skew means it is really a directional bet.
        "buy_sell_ratio": (float(buy_shares / sell_shares) if sell_shares else float("nan")),
        # PARTICIPATION RATE: our traded shares as a share of ALL shares traded.
        # Read this before you believe any PnL number. The simulated crowd does
        # not react to our presence, so if we are taking 30% of the volume the
        # backtest is describing a market that would not exist if we were in it.
        # A real single-venue market maker in a liquid name is more like 1-10%.
        "participation_pct": (100.0 * filled_shares / result.meta["market_volume"]
                              if result.meta.get("market_volume") else float("nan")),
        "mean_queue_ahead_at_entry": float(result.meta.get("mean_queue_ahead", np.nan)),
        "n_fair_value_jumps": float(result.meta.get("n_jumps", 0)),
        "risk_check_rejections": float(result.meta.get("n_risk_rejects", 0)),

        # --- risk -----------------------------------------------------------
        "mean_inventory": float(np.mean(grid_inv)) if grid_inv.size else float("nan"),
        "stdev_inventory": float(np.std(grid_inv)) if grid_inv.size else float("nan"),
        "max_abs_inventory": float(np.max(np.abs(grid_inv))) if grid_inv.size else float("nan"),
        "final_inventory": float(result.meta.get("final_inventory", np.nan)),
        "pct_time_flat": (float(np.mean(np.abs(grid_inv) < 1e-9) * 100)
                          if grid_inv.size else float("nan")),

        # --- PnL attribution -------------------------------------------------
        "spread_capture_total": spread_capture,
        "spread_capture_per_share": spread_capture_per_share,
        "fees_total": float(total_fees),
        "rebate_income": float(-total_fees),
        "pnl_ex_rebates": (float(grid_eq[-1]) + float(total_fees)) if grid_eq.size else float("nan"),
    }
    stats.update(markouts(fills, grid_t, grid_mid, horizons, tick))
    return stats


def format_report(stats: Dict[str, float], title: str = "BACKTEST RESULTS") -> str:
    """Human-readable report, grouped so it can be read top-to-bottom."""
    groups = [
        ("HEADLINE", ["final_pnl", "pnl_per_hour", "sharpe_annualised",
                      "max_drawdown", "return_over_maxdd"]),
        ("ACTIVITY", ["sim_hours", "n_market_events", "n_fills", "fills_per_hour",
                      "filled_shares", "quotes_placed", "quotes_cancelled",
                      "fill_rate_pct", "buy_sell_ratio", "participation_pct",
                      "mean_queue_ahead_at_entry", "n_fair_value_jumps",
                      "risk_check_rejections"]),
        ("INVENTORY RISK", ["mean_inventory", "stdev_inventory",
                            "max_abs_inventory", "final_inventory", "pct_time_flat"]),
        ("PNL ATTRIBUTION", ["spread_capture_total", "spread_capture_per_share",
                             "rebate_income", "pnl_ex_rebates"]),
        ("MARKOUTS  (= spread capture + post-trade drift)",
         [k for k in stats if k.startswith("markout_") and k.endswith("per_share")]),
        ("ADVERSE SELECTION  (post-trade drift alone; negative is normal)",
         [k for k in stats if k.startswith("adverse_sel_") and k.endswith("per_share")]),
    ]
    lines = ["", "=" * 66, f" {title}", "=" * 66]
    for name, keys in groups:
        lines.append(f"\n  {name}")
        lines.append("  " + "-" * 62)
        for k in keys:
            if k in stats:
                v = stats[k]
                lines.append(f"    {k:<34} {v:>18,.4f}")
    lines.append("=" * 66)
    return "\n".join(lines)
