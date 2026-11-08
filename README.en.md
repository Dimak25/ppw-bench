# PPW Bench — Performance per Watt

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey)](#requirements)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![CI](https://github.com/Dimak25/ppw-bench/actions/workflows/ci.yml/badge.svg)](https://github.com/Dimak25/ppw-bench/actions/workflows/ci.yml)

[Русский](README.md) | **English**

Measures a CPU's **PPW (Performance per Watt)**: how much work it does per
second for every watt it consumes.

```
PPW = (work / second) / (average power, W)
```

One script runs a deterministic C workload, samples real CPU power (RAPL) at the
same time, and saves a `.txt` report. Power comes from Windows performance
counters or from Linux `powercap`. Built for the Intel Core Ultra 9 185H, but
works on any Windows or Linux PC/laptop that exposes energy counters
(Intel, AMD Zen).

## Features

* C workload (Win32 threads / pthreads): integer chains + floating point,
  identical work on every machine, so scores are comparable across runs.
  The compiler version is recorded in the report.
* Multi-thread and single-thread phases.
* Power: RAPL (CPU package and cores) with a system-wide fallback
  (`Power Meter` on Windows, `psys` / battery on Linux). On Linux the energy
  counter is used directly (ΔE/Δt), which is more accurate than averaging samples.
* A cooldown between the multi-thread and single-thread phases.
* Proper warm-up handling: the PL2 boost is skipped, the steady state is measured.
* Stability indicator (power and speed spread) — you can see whether to trust the number.
* Warnings: running on battery, power-saving plan/profile, background CPU load.
* `.txt` report (on Windows UTF-8 with BOM — opens correctly in Notepad).
* No external Python dependencies.

## Requirements

### Windows

* Windows 10/11.
* Python 3.9+ (`py` or `python` in PATH).
* **gcc** — MinGW-w64 or TDM-GCC (`gcc --version`).
  `bench.exe` is built automatically on the first run.

### Linux

* Any x86_64 distribution, kernel with `intel_rapl_common` (Intel, AMD Zen).
* Python 3.9+ (`python3`) and `gcc` — or the ready-made release, see below.
* RAPL access: since kernel 5.10 `energy_uj` is readable by root only, so run
  with `sudo` or install the udev rule once (see [Linux](#linux)).

For a fair measurement: AC power, "Balanced" or "High performance" power plan
(on Linux: `powerprofilesctl set balanced`), other applications closed.

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

## Linux

From the repository:

```bash
git clone https://github.com/Dimak25/ppw-bench.git
cd ppw-bench
sudo python3 ppw_bench.py --quick   # bench is built with gcc automatically
```

Ready-made builds from [Releases](https://github.com/Dimak25/ppw-bench/releases/latest):

* `ppw-bench-vX.Y.Z-linux-x86_64.tar.gz` — the script plus a static `bench`
  (~400 KB). Needs only `python3` >= 3.9, which ships with almost every distro.
* `ppw-bench-vX.Y.Z-linux-x86_64` — a standalone binary built with Nuitka, no
  Python needed (~4 MB, glibc >= 2.35: Ubuntu 22.04+, Debian 12+, Fedora 36+).

```bash
tar xzf ppw-bench-v*-linux-x86_64.tar.gz
cd ppw-bench-v*-linux-x86_64
sudo ./ppw-bench --quick
```

```bash
chmod +x ppw-bench-v*-linux-x86_64
sudo ./ppw-bench-v*-linux-x86_64 --quick
```

When run with `sudo`, the report is handed back to your user, not root.

### RAPL without sudo (udev)

The rule gives read access to `energy_uj` to the `powercap` group only (not to
everyone: the counters are restricted because of the Platypus side-channel
attack, CVE-2020-8694), and the script adds you to that group:

```bash
sudo ./install-udev.sh        # in the repository: sudo packaging/linux/install-udev.sh
```

Then log out and back in (or run `newgrp powercap`). To remove it, run
`sudo ./install-udev.sh --uninstall`.

## Portable build for Windows (no Python or gcc)

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
| `--cooldown N` | pause between phases in seconds (default 10, 0 = none) |
| `--pin MODE` | cores for the single-thread phase: `auto` (default), `p`, `e`, `lpe`, `all` (a separate phase on each core type), `none` or a CPU list (`0,2,4-7`) |
| `--repeat N` | run each phase N times; the report shows medians and the run-to-run spread |
| `--watts N` | use a fixed CPU wattage (no sensors available) |
| `--out FILE` | report path (default `ppw-report-*.txt`) |
| `--list` | list detected sensors and exit |
| `--interactive` | open the interactive menu |
| `--version` | print the version and exit |

### Hybrid CPUs and repeats

Hybrid CPUs (Intel 12th gen and newer, Core Ultra, AMD Zen 5 + Zen 5c) mix
P (performance) and E (efficiency) cores. The OS scheduler moves a
single-threaded load between them, so the ST result can swing by ~10 % between
runs.

* `--pin auto` (default) — on a hybrid CPU the ST phase runs on P-cores only;
  nothing changes on a regular CPU. The report's "Ядра" (cores) line shows
  which cores were used.
* `--pin e` / `--pin lpe` — single-thread test on E- or LP-E-cores (LP-E
  cores exist on Core Ultra series 1, Meteor Lake); `--pin none` — the old behaviour.
* `--pin all` — the single-thread phase runs on each core type in turn
  (P → E → LP-E); the summary shows `ST (P)`, `ST (E)`, `ST (LP-E)`. In the
  interactive menu this is item 5 (shown on hybrid CPUs only).

  ```bat
  ppw-bench.exe --only-st --pin all --duration 30 --repeat 3
  ```

  Power is whole-package power (idle cores and the rest of the die included),
  so PPW shows the efficiency of "the laptop running on one core of that type",
  not of an isolated core.

  How core types are detected: Windows — the core `EfficiencyClass`; Linux —
  `cpu_core` / `cpu_atom`. Both OSes often group E and LP-E together; they are
  told apart by cache: LP-E cores (on the SoC tile) share no L3 with P-cores.
* `--repeat 3` — every phase three times; the report shows medians and a
  "Повторы" (repeats) line with the run-to-run spread. If two machines differ
  by less than that spread, treat them as equal.

```bat
ppw-bench.exe --quick --repeat 3
```

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
* **Core frequency** ("Частота ядра / ядер") — effective frequency of the
  cores the phase ran on: the busiest core (and its number) in the
  single-thread phase; the average over busy cores in the multi-thread phase,
  split into P / E / LP-E on hybrid CPUs. Windows: `% Processor Performance` ×
  `Processor Frequency` (like Task Manager's "Speed"); Linux:
  `scaling_cur_freq` (or `cpu MHz` from `/proc/cpuinfo`). Shows whether the
  power plan keeps cores at a low clock.
* **Stability** — spread inside the measurement window. Above ~10–15 % the run
  is noisy (battery, power saving, background tasks).

## How it works

1. `ppw_bench.py` builds `bench.c` into `bench.exe` / `bench` when needed.
2. It starts a phase; the C workload prints progress once per second.
3. A background thread reads power sensors:
   * **Windows**, PDH counters: `\Energy Meter(*)\Power` (RAPL package/cores),
     `\Power Meter(*)\Power` (system);
   * **Linux**, `powercap`: `intel-rapl:N/energy_uj` (`package-N`), the `core`
     subzone, `psys`; battery `power_now` as a fallback.
4. The steady-state window after warm-up is selected; power and throughput are
   averaged over that same window.
5. `PPW = work/s ÷ watts`; the report is written to `.txt`.

## Project layout

```
ppw-bench/
├── ppw_bench.py            # entry point
├── ppwbench/
│   ├── cli.py              # command line, menu
│   ├── core.py             # bench build, phases, PPW
│   ├── report.py           # report
│   └── platforms/
│       ├── windows.py      # PDH, registry, powercfg
│       └── linux.py        # powercap, /proc, /sys
├── bench.c                 # C workload (Win32 threads / pthreads)
├── tests/                  # pytest
├── run.bat                 # double-click launcher (Windows)
├── build-portable.ps1      # Windows: portable exe + ZIP
├── build-linux.sh          # Linux: tar.gz + Nuitka onefile
├── packaging/linux/        # udev rule, install-udev.sh, launcher
├── README.md / README.en.md
├── LICENSE                 # MIT
└── .github/workflows/
    ├── ci.yml              # build + smoke test
    └── release.yml         # publish the portable release on a tag
```

`bench.exe` / `bench` are build artifacts and are not committed (see `.gitignore`).

## Troubleshooting

* `--list` is empty or says "no PDH sensors" — the `Energy Meter` provider is
  missing. Use `--watts N` (CPU watts) or an external power meter.
* Sensor present but PPW is empty — it returned 0; check `--list`.
* `gcc not found` — install MinGW-w64/TDM-GCC or build `bench.exe` manually.
* Linux: "RAPL found but not readable" — run with `sudo` or install the udev rule.
* Linux: no `intel-rapl` zones — `sudo modprobe intel_rapl_common`. RAPL is
  usually unavailable inside virtual machines.

## Tips for comparable results

1. Use AC power (on battery the CPU is power-limited).
2. Power plan: "Balanced" or "High performance".
3. Close browsers/IDEs/antivirus scans.
4. Keep `--duration` and the thread count identical.
5. To compare hardware, use the multi-thread phase with all logical processors.

## CI

GitHub Actions on `windows-latest` and `ubuntu-latest` builds `bench.c`, runs
the unit tests and a short smoke test. Power sensors are unavailable on CI runners, so PPW is n/a
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
