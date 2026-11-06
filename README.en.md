# PPW Bench — Performance per Watt

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)](#requirements)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![CI](https://github.com/Dimak25/ppw-bench/actions/workflows/ci.yml/badge.svg)](https://github.com/Dimak25/ppw-bench/actions/workflows/ci.yml)

[Русский](README.md) | **English**

Measures a CPU's **PPW (Performance per Watt)**: how much work it does per
second for every watt it consumes.

```
PPW = (work / second) / (average power, W)
```

One script runs a deterministic C workload, samples real CPU power from Windows
performance counters (RAPL) at the same time, and saves a `.txt` report. Built for
the Intel Core Ultra 9 185H, but works on any Windows PC/laptop that exposes
energy counters.

## Features

* C workload (Win32 threads): integer chains + floating point, identical work on
  every machine, so scores are comparable across runs.
* Multi-thread and single-thread phases.
* Power: RAPL (CPU package and cores) with a system-wide `Power Meter` fallback.
* Proper warm-up handling: the PL2 boost is skipped, the steady state is measured.
* Stability indicator (power and speed spread) — you can see whether to trust the number.
* Warnings: running on battery, "Power saver" plan, background CPU load.
* `.txt` report (UTF-8 with BOM — opens correctly in Notepad).
* No external Python dependencies.

## Requirements

* Windows 10/11.
* Python 3.9+ (`py` or `python` in PATH).
* **gcc** — MinGW-w64 or TDM-GCC (`gcc --version`).
  `bench.exe` is built automatically on the first run.

For a fair measurement: AC power, "Balanced" or "High performance" power plan,
other applications closed.

## Install

```bat
git clone https://github.com/Dimak25/ppw-bench.git
cd ppw-bench
py ppw_bench.py
```

The build happens automatically. Manual build:

```bat
gcc -O2 -static bench.c -o bench.exe
```

## Portable build (no Python or gcc)

Ready-to-run builds are available on the
[Releases](https://github.com/Dimak25/ppw-bench/releases/latest) page — Python
and gcc are not required.

* `ppw-bench.exe` — a single file, the most convenient option.
* `ppw-bench-vX.Y.Z-win64-portable.zip` — a folder with the exe and `_internal`
  (**recommended if your antivirus flags the single exe**).

1. Download `ppw-bench.exe` or the ZIP.
2. Run it by double-clicking or from a terminal:

   ```bat
   ppw-bench.exe --quick
   ```

3. The `ppw-report-*.txt` report is written next to the exe.

### If antivirus reports Trojan:Win32/Wacatac.H!ml

This is a known heuristic false positive on unsigned PyInstaller onefile builds.
The program contains no network code or bundled third-party binaries.

1. Verify the downloaded file's SHA256 against `SHA256SUMS.txt` from the release.
2. Use the ZIP build — it is less likely to trigger such heuristics.
3. Submit the file to Microsoft as a false positive:
   <https://www.microsoft.com/en-us/wdsi/filesubmission>.
4. If needed, add the program folder to Defender exclusions.

> The exe is not code-signed, so Windows SmartScreen may also warn about an
> unknown publisher. This is expected for an open-source build: verify SHA256
> against `SHA256SUMS.txt` from the release.

## Usage

```bat
py ppw_bench.py                 :: 60 s per phase, MT + ST
py ppw_bench.py --quick         :: 20 s per phase
py ppw_bench.py --duration 120  :: more stable (PL1)
py ppw_bench.py --list          :: list power sensors and exit
```

Or double-click `run.bat` / `ppw-bench.exe`.

With no arguments an interactive menu opens: show power sensors, quick test
(20 s), normal (60 s) or long (120 s). After the test the app asks whether to
save the `.txt` report and keeps the window open.

| Option | Description |
|--------|-------------|
| `--duration N` | seconds per phase (default 60; 120 is more stable) |
| `--quick` | fast mode: 20 s per phase |
| `--threads N` | threads in the multi-thread phase (default: all) |
| `--no-st` | multi-thread phase only |
| `--only-st` | single-thread phase only |
| `--watts N` | use a fixed CPU wattage (no sensors available) |
| `--out FILE` | report path (default `ppw-report-*.txt`) |
| `--list` | list detected sensors and exit |
| `--interactive` | open the interactive menu |
| `--version` | print the version and exit |

## Sample output

```
PHASE 1 — MULTI-THREAD (22 threads)
  Throughput          : 1.541 G units/s
  Avg CPU power       : 15.92 W (package)
  Avg core power      : 11.38 W (pp0)
  Avg system power    : 22.03 W
  PPW (package)       : 96.772 M units/s per W
  PPW (system)        : 69.941 M units/s per W
  Stability           : power ±3.0%, throughput ±3.1%
```

* **Throughput** — work units per second. One unit is a fixed block of
  computation in `bench.c`, so results are comparable between machines and runs.
* **PPW (package)** — the main result: work/s per watt of CPU package power.
* **PPW (system)** — the same for whole-system power (on battery).
* **Stability** — spread inside the measurement window. Above ~10–15 % the run
  is noisy (battery, power saving, background tasks).

## How it works

1. `ppw_bench.py` builds `bench.c` into `bench.exe` when needed.
2. It starts a phase; the C workload prints progress once per second.
3. A background thread reads PDH counters:
   * `\Energy Meter(*)\Power` — RAPL package/core power;
   * `\Power Meter(*)\Power` — system power;
   * `\Processor Information(_Total)\% Processor Performance` — auxiliary.
4. The steady-state window after warm-up is selected; power and throughput are
   averaged over that same window.
5. `PPW = work/s ÷ watts`; the report is written to `.txt`.

## Project layout

```
ppw-bench/
├── ppw_bench.py            # runner, power sampling, PPW, report
├── bench.c                 # C workload (Win32 threads)
├── run.bat                 # double-click launcher
├── build-portable.ps1      # portable exe + ZIP build
├── README.md / README.en.md
├── LICENSE                 # MIT
└── .github/workflows/
    ├── ci.yml              # build + smoke test
    └── release.yml         # publish the portable release on a tag
```

`bench.exe` is a build artifact and is not committed (see `.gitignore`).

## Troubleshooting

* `--list` is empty or says "no PDH sensors" — the `Energy Meter` provider is
  missing. Use `--watts N` (CPU watts) or an external power meter.
* Sensor present but PPW is empty — it returned 0; check `--list`.
* `gcc not found` — install MinGW-w64/TDM-GCC or build `bench.exe` manually.

## Tips for comparable results

1. Use AC power (on battery the CPU is power-limited).
2. Power plan: "Balanced" or "High performance".
3. Close browsers/IDEs/antivirus scans.
4. Keep `--duration` and the thread count identical.
5. To compare hardware, use the multi-thread phase with all logical processors.

## CI

GitHub Actions on `windows-latest` builds `bench.c` with MinGW-w64 and runs a
short smoke test. Power sensors are unavailable on CI runners, so PPW is n/a
there — the pipeline only validates that everything runs.

## License

[MIT](LICENSE) © 2026 Dimak25

## About this project

The project was built with the help of a neural network (AI assistant) and was
originally created for personal use — comparing laptops with different CPUs by
efficiency (performance per watt). Anyone is free to use it: the code is open,
licensed under MIT, so fork it, change it and adapt it to your needs.

The author does not guarantee correct measurements on every machine: the set of
available power sensors depends on the platform and drivers. Check the numbers on
your own hardware — `--list` shows what is available.
