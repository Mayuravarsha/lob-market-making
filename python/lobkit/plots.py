"""Charts for the backtest.

Deliberately plain matplotlib, one figure per idea. The point of each chart is
written in its title so a reader knows what they are supposed to notice.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

import numpy as np

import matplotlib
matplotlib.use("Agg")            # render to files, no display needed
import matplotlib.pyplot as plt  # noqa: E402

BUY, SELL = 0, 1

INK = "#1f2933"
ACCENT = "#2563eb"
WARM = "#c2410c"
GREEN = "#15803d"
GREY = "#94a3b8"

plt.rcParams.update({
    "figure.dpi": 130,
    "savefig.dpi": 130,
    "font.size": 9,
    "axes.edgecolor": "#cbd5e1",
    "axes.labelcolor": INK,
    "text.color": INK,
    "xtick.color": "#64748b",
    "ytick.color": "#64748b",
    "axes.grid": True,
    "grid.color": "#e2e8f0",
    "grid.linewidth": 0.6,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.facecolor": "white",
})


def _save(fig, outdir: str, name: str) -> str:
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, name)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_backtest(result, stats: Dict[str, float], outdir: str,
                  prefix: str = "backtest") -> Dict[str, str]:
    paths = {}
    t_min = result.t / 60.0

    # --- 1. the four-panel summary ------------------------------------------
    fig, ax = plt.subplots(4, 1, figsize=(9, 10), sharex=True,
                           gridspec_kw={"height_ratios": [2, 1.4, 1.2, 1]})

    ax[0].plot(t_min, result.equity, color=ACCENT, lw=1.1)
    ax[0].axhline(0, color=GREY, lw=0.8, ls="--")
    peak = np.maximum.accumulate(result.equity)
    ax[0].fill_between(t_min, result.equity, peak, color=WARM, alpha=0.18,
                       label="drawdown")
    ax[0].set_ylabel("PnL ($)")
    ax[0].set_title(
        f"Mark-to-market PnL   |   final ${stats['final_pnl']:,.0f}   "
        f"max DD ${stats['max_drawdown']:,.0f}   "
        f"{stats['n_fills']:,.0f} fills   "
        f"{stats['participation_pct']:.1f}% of volume", loc="left")
    ax[0].legend(frameon=False, loc="upper left")

    ax[1].plot(t_min, result.inventory, color=WARM, lw=0.9)
    ax[1].axhline(0, color=GREY, lw=0.8)
    ax[1].set_ylabel("inventory\n(shares)")
    ax[1].set_title("Inventory: skew should keep this oscillating around zero, "
                    "not trending", loc="left")

    ax[2].plot(t_min, result.mid, color=INK, lw=0.8)
    ax[2].set_ylabel("mid (ticks)")
    ax[2].set_title("Mid price", loc="left")

    ax[3].plot(t_min, result.spread, color=GREEN, lw=0.6, alpha=0.8)
    ax[3].set_ylabel("spread\n(ticks)")
    ax[3].set_xlabel("simulated time (minutes)")
    ax[3].set_title("Bid-ask spread", loc="left")
    paths["summary"] = _save(fig, outdir, f"{prefix}_summary.png")

    # --- 2. PnL attribution --------------------------------------------------
    fig, axs = plt.subplots(1, 2, figsize=(10, 3.6))
    comps = [
        ("spread\ncaptured", stats.get("spread_capture_total", 0), GREEN),
        ("adverse\nselection", stats.get("adverse_sel_30.0s_total", 0), WARM),
        ("rebates", stats.get("rebate_income", 0), ACCENT),
    ]
    names = [c[0] for c in comps]
    vals = [c[1] for c in comps]
    cols = [c[2] for c in comps]
    axs[0].bar(names, vals, color=cols, alpha=0.85)
    axs[0].axhline(0, color=INK, lw=0.8)
    axs[0].set_ylabel("$")
    axs[0].set_title("Where the money came from\n(sum ~ final PnL)", loc="left")
    for i, v in enumerate(vals):
        axs[0].text(i, v, f"${v:,.0f}", ha="center",
                    va="bottom" if v >= 0 else "top", fontsize=8)

    hs = sorted({float(k.split("_")[2].replace("s", ""))
                 for k in stats if k.startswith("adverse_sel_") and k.endswith("per_share")})
    if hs:
        mo = [stats[f"markout_{h}s_per_share"] * 100 for h in hs]
        ad = [stats[f"adverse_sel_{h}s_per_share"] * 100 for h in hs]
        axs[1].plot(hs, mo, "o-", color=GREEN, label="net markout")
        axs[1].plot(hs, ad, "o-", color=WARM, label="adverse selection")
        axs[1].axhline(0, color=GREY, lw=0.8, ls="--")
        axs[1].set_xscale("log")
        axs[1].set_xlabel("horizon after fill (seconds, log scale)")
        axs[1].set_ylabel("cents per share")
        axs[1].set_title("Markout curve\nnet must stay above zero", loc="left")
        axs[1].legend(frameon=False)
    paths["attribution"] = _save(fig, outdir, f"{prefix}_attribution.png")

    # --- 3. fill and inventory distributions ---------------------------------
    fig, axs = plt.subplots(1, 3, figsize=(12, 3.4))
    if result.fills:
        rel = np.array([(f["mid_at_fill"] - f["price"]) *
                        (1 if f["side"] == BUY else -1) for f in result.fills])
        axs[0].hist(rel, bins=40, color=ACCENT, alpha=0.85)
        axs[0].axvline(0, color=WARM, lw=1)
        axs[0].set_xlabel("ticks of edge vs pre-trade mid")
        axs[0].set_title("Edge captured per fill\n(left of 0 = paid up)", loc="left")

        qa = np.array([f.get("queue_ahead_at_entry", -1) for f in result.fills])
        qa = qa[qa >= 0]
        if qa.size:
            axs[1].hist(qa, bins=40, color=GREEN, alpha=0.85)
            axs[1].set_xlabel("shares queued ahead when we joined")
            axs[1].set_title("Queue position of filled orders\n"
                             "small = we were near the front", loc="left")

    axs[2].hist(result.inventory, bins=40, color=WARM, alpha=0.85,
                orientation="horizontal")
    axs[2].axhline(0, color=INK, lw=1)
    axs[2].set_ylabel("inventory (shares)")
    axs[2].set_title("Time spent at each\ninventory level", loc="left")
    paths["distributions"] = _save(fig, outdir, f"{prefix}_distributions.png")
    return paths


def plot_book_snapshot(book, outdir: str, n_levels: int = 10,
                       name: str = "book_snapshot.png") -> str:
    """The classic depth chart: bids left, asks right."""
    bids = book.depth(BUY, n_levels)
    asks = book.depth(SELL, n_levels)
    fig, ax = plt.subplots(figsize=(8, 3.4))
    if bids:
        ax.bar([b[0] for b in bids], [b[1] for b in bids],
               color=GREEN, alpha=0.85, width=0.8, label="bids")
    if asks:
        ax.bar([a[0] for a in asks], [a[1] for a in asks],
               color=WARM, alpha=0.85, width=0.8, label="asks")
    ax.set_xlabel("price (ticks)")
    ax.set_ylabel("resting size (shares)")
    ax.set_title("Level-2 depth snapshot", loc="left")
    ax.legend(frameon=False)
    return _save(fig, outdir, name)


def plot_strategy_comparison(rows, outdir: str,
                             name: str = "strategy_comparison.png") -> str:
    """rows: list of (label, stats dict)."""
    labels = [r[0] for r in rows]
    x = np.arange(len(labels))
    fig, axs = plt.subplots(1, 4, figsize=(14, 3.4))
    for ax, key, title, col in [
        (axs[0], "final_pnl", "Final PnL ($)", ACCENT),
        (axs[1], "sharpe_annualised", "Sharpe (annualised)", GREEN),
        (axs[2], "max_drawdown", "Max drawdown ($)", WARM),
        (axs[3], "stdev_inventory", "Inventory stdev (shares)", INK),
    ]:
        vals = [r[1].get(key, np.nan) for r in rows]
        ax.bar(x, vals, color=col, alpha=0.85)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=8)
        ax.set_title(title, loc="left")
        ax.axhline(0, color=GREY, lw=0.8)
    return _save(fig, outdir, name)


def plot_latency_histogram(samples_ns, outdir: str,
                           name: str = "latency_hist.png") -> str:
    s = np.asarray(samples_ns, dtype=float)
    fig, ax = plt.subplots(figsize=(8, 3.2))
    ax.hist(s[s < np.percentile(s, 99.5)], bins=120, color=ACCENT, alpha=0.85)
    for p, c in [(50, GREEN), (99, WARM)]:
        v = np.percentile(s, p)
        ax.axvline(v, color=c, lw=1.2, label=f"p{p} = {v:.0f} ns")
    ax.set_xlabel("nanoseconds per operation")
    ax.set_ylabel("count")
    ax.set_title("Order-book operation latency (below p99.5)", loc="left")
    ax.legend(frameon=False)
    return _save(fig, outdir, name)


def plot_vol_signature(intervals, est_vol, true_vol, outdir: str,
                       name: str = "volatility_signature.png") -> str:
    """The volatility signature plot.

    Realised volatility estimated from mid prices, as a function of how often
    you sample. Microstructure noise inflates the estimate at fast sampling; the
    curve should flatten out toward the true value as the interval grows.
    """
    fig, ax = plt.subplots(figsize=(7.5, 3.6))
    ax.plot(intervals, est_vol, "o-", color=ACCENT, label="estimated from mid")
    ax.axhline(true_vol, color=WARM, ls="--", lw=1.2,
               label=f"true fair-value vol = {true_vol:.2f}")
    ax.set_xscale("log")
    ax.set_xlabel("sampling interval (seconds, log scale)")
    ax.set_ylabel("ticks per sqrt(second)")
    ax.set_title("Volatility signature plot: sampling too fast measures noise, "
                 "not volatility", loc="left")
    ax.legend(frameon=False)
    return _save(fig, outdir, name)
