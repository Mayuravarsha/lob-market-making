# Limit Order Book & Market-Making Backtester

A price-time-priority matching engine in C++17, exposed to Python with pybind11,
plus an event-driven market-making backtester that puts its own orders into the
same book as the simulated crowd — so queue position and fills are real rather
than modelled.

**New to any of this?** Read [`docs/CONCEPTS.md`](docs/CONCEPTS.md) first. It
starts from "what happens when you buy a stock" and ends at adverse-selection
decomposition, assuming no prior knowledge.
[`docs/INTERVIEW.md`](docs/INTERVIEW.md) is the same material in Q&A form.

---

## Headline results

The table below was re-measured on **AMD Ryzen 9 5900HS (8 cores / 16 threads),
Windows 11, GCC 16.2.0 (MinGW-w64 UCRT, WinLibs build), Release build with
Ninja** — a portable toolchain, no installer, no admin rights. It replaces an earlier table measured on a 2 vCPU / 2.8 GHz
cloud VM with a different (Linux/glibc) compiler; **the two are not
comparable** — different core count, different clock behavior, different
compiler. See `results/benchmark_results.txt` and `results/latency_profile.txt`
for the raw tool output.

### Matching engine

| Operation | Median (p50) | p99 | Throughput |
|---|---|---|---|
| add limit (passive) | <100 ns\* | 200 ns | 10.2 M/s |
| cancel | <100 ns\* | 200 ns | 12.5 M/s |
| modify (shrink, keeps priority) | — | — | 61.0 M/s |
| modify (reprice, loses priority) | — | — | 6.52 M/s |
| market order, one fill | 100 ns | 400 ns | 7.24 M/s |
| microprice read | <100 ns\* | <100 ns\* | not in this build's benchmark suite (see note) |
| **realistic mixed flow** (55% add / 35% cancel / 10% aggressive) | 100 ns | 1,200 ns | **3.62 M events/s** |

\* On this machine `std::chrono::steady_clock` (via Windows'
`QueryPerformanceCounter`) ticks in 100 ns increments — coarser than several
of the operations being timed. A p50 of "<100 ns" means "faster than one
clock tick," not "measured as zero." The original table's Linux box had
~5–20 ns clock resolution, which is the biggest single reason these numbers
look different from the old ones — not a regression. Full explanation in
`results/latency_profile.txt`. `bench_order_book`'s current suite also no
longer has a standalone microprice throughput case (replaced by
`BM_TopOfBook` at 265 M/s and `BM_Depth10` at 5.31 M/s); only the
latency-profile p50/p99 for microprice carried over.

Pre-faulting the pool still removes the page-fault tail, though the 100 ns
clock granularity above hides it at p50/p99 on this machine — it shows up
further out: add-limit p99.9 is 5,600 ns without `--prefault` vs. 600 ns with
it (**~9×**), and max drops from 186,200 ns to 112,700 ns. Re-run
`./build/latency_profile.exe 1500000` (no flag) yourself to reproduce the
unfaulted numbers; `results/latency_profile.txt` only has the `--prefault`
run on file.

### Correctness

- **81** unit-test checks over every matching rule (`./build/test_order_book`)
- **200,000**-operation invariant fuzz
- **200,000**-event differential test against an independent pure-Python
  implementation (`scripts/04_differential_test.py`'s default): **0
  mismatches** on every observable, checked after every event — re-verified
  on this machine's MinGW-built C++ module, not just the reference pair

### Python bindings

| approach | ns/event | events/s |
|---|---|---|
| `replay_batch()` — one boundary crossing | 111 | **9.03 M** |
| per-event calls | 1,178 | 0.85 M |

**10.6×**, identical work. ~1,067 ns/event of the per-event figure is pure
pybind11 overhead.

### Market-making backtest

400,000 events ≈ 51 minutes of simulated market time, inventory-skew strategy:

| | |
|---|---|
| fills | 24,505 (1.02 M shares) |
| participation | 9.2% of all volume |
| mean queue ahead at entry | 157 shares |
| **spread captured** | **+1.40 ¢/share** |
| **adverse selection (1 s)** | **−1.05 ¢/share** |
| **maker rebate** | **+0.20 ¢/share** |
| net | **+0.55 ¢/share** |
| inventory std dev | 53 shares |
| max drawdown | $55 |

The reported annualised Sharpe (~1,584) is **not a real number** and the code
says so in its own output. See `docs/CONCEPTS.md` §13–14 for exactly why, and for
the more defensible cross-run figure.

---

## Quick start

### Python only (no compiler needed)

```bash
cd lob-market-making
pip install numpy matplotlib
python scripts/01_run_backtest.py --events 200000
```

Everything runs on the pure-Python fallback book. It is slower but produces
identical results.

### With the C++ engine

Needs CMake ≥ 3.16 and a C++17 compiler. pybind11 and Google Benchmark are
downloaded automatically at configure time (pinned versions, no manual install).

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j

./build/test_order_book                 # 81 correctness checks
./build/bench_order_book                # Google Benchmark suite
./build/latency_profile 1500000 --prefault   # percentile latencies

python scripts/01_run_backtest.py --events 400000
python scripts/02_experiments.py --events 120000 --runs 10
python scripts/03_boundary_benchmark.py
python scripts/04_differential_test.py
```

<details>
<summary><b>Windows setup, step by step</b></summary>

**Option A — Visual Studio (most straightforward on Windows)**

1. Install [Visual Studio Community](https://visualstudio.microsoft.com/downloads/).
   In the installer, tick **Desktop development with C++**. That gives you the
   MSVC compiler and CMake.
2. Open **x64 Native Tools Command Prompt for VS** from the Start menu.
3. ```
   cd path\to\lob-market-making
   cmake -S . -B build
   cmake --build build --config Release
   ```
4. The Python module lands in `python\lobkit\`. Run the scripts normally.

**Option B — WSL (closest to what trading firms actually use)**

1. In PowerShell as administrator: `wsl --install`, then reboot.
2. In the Ubuntu terminal:
   ```
   sudo apt update && sudo apt install -y build-essential cmake python3-pip git
   pip3 install numpy matplotlib
   cd /mnt/c/path/to/lob-market-making
   cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
   ```

**Option C — MSYS2 / MinGW**

```
pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake
cmake -S . -B build -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
```

If the build fails, the Python fallback still runs everything — nothing is
blocked.
</details>

---

## Layout

```
include/lob/          the engine, header by header
  types.hpp             integer ticks, sides, trades, exec reports
  order.hpp             the Order struct and the pool allocator (+ prefault)
  price_level.hpp       one price's FIFO queue, intrusive linked list
  order_book.hpp        the book: two maps, an id index, and the matching rules
src/order_book.cpp    matching, cancel, modify, depth, batched replay
bindings/py_lob.cpp   pybind11 module, including the batched replay path
tests/                81 correctness checks + a 200k-op invariant fuzz
bench/
  bench_order_book.cpp  Google Benchmark, with chunked state refills
  latency_profile.cpp   per-operation percentiles, clock overhead removed

python/lobkit/
  __init__.py           picks the C++ book, falls back to Python
  pybook.py             independent reference implementation
  simulate.py           Cont-Stoikov-Talreja flow + informed traders + jumps
  strategy.py           inventory skew and Avellaneda-Stoikov
  backtest.py           event loop, fills from the real matching engine
  metrics.py            Sharpe, drawdown, fill rate, markouts, participation
  lobster.py            real NASDAQ Level-2 loader + reconstruction validator
  plots.py

scripts/
  01_run_backtest.py        one full run, report and charts
  02_experiments.py         ablations, sweep, cross-run Sharpe, vol signature
  03_boundary_benchmark.py  what pybind11 costs
  04_differential_test.py   C++ vs Python, event by event
  05_lobster_real_data.py   replay real NASDAQ data and verify against it

docs/CONCEPTS.md      everything explained from zero
docs/INTERVIEW.md     Q&A, including the bug stories
results/              charts and JSON from the runs above
```

---

## Using real data

Free NASDAQ Level-2 samples: <https://lobsterdata.com/info/DataSamples.php>

Download any sample day, unzip both CSVs into `data/`, then:

```bash
python scripts/05_lobster_real_data.py
```

It replays every message through the C++ book and — the part that matters —
compares the reconstructed top of book against LOBSTER's own snapshot file at
regular checkpoints. "Replayed Level-2 data" is a claim; "verified my
reconstruction matched the exchange's own book state at every checkpoint" is
evidence.

---

## What this is not

Stated up front, because knowing the limits is the point:

- The simulated crowd **does not react to our quotes**. No market impact, no
  competing market maker undercutting us. This is the single biggest gap and it
  is why participation % is reported so prominently.
- Latency is a constant, not a random variable with a tail.
- One instrument, one venue. No Reg NMS routing, no fragmentation.
- No persistence, recovery, FIX gateway or multithreading in the engine. It is
  clean, tested and fast — it is not a production exchange.

`docs/CONCEPTS.md` §14 has the full list.
