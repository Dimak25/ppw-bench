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
  Python needed (~6 MB, glibc >= 2.35: Ubuntu 22.04+, Debian 12+, Fedora 36+).

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

With no arguments a two-step interactive menu opens:

1. **Which test:** whole CPU (multi-thread + single-thread on each core type:
   P / E / LP-E), multi-thread only, single-thread only, or instruction sets
   (scalar, AVX2+FMA, AES-NI). Power sensors can be shown from here too.
2. **Seconds per phase:** 20, 60 or 120 s — the same for every phase. The
   phase count and estimated total time are shown next to each option.
3. **Power sweep:** no, the default 5–45 W steps, or your own (see
   [below](#power-sweep)). On Windows, step 4 then picks automatic or manual
   limits.

After the test the app asks whether to save the `.txt` report and keeps the
window open.

| Option | Description |
|--------|-------------|
| `--duration N` | seconds per phase (default 60; 120 is more stable) |
| `--quick` | fast mode: 20 s per phase |
| `--threads N` | threads in the multi-thread phase (default: all) |
| `--no-st` | multi-thread phase only |
| `--only-st` | single-thread phase only |
| `--cooldown N` | pause between phases in seconds (default 10, 0 = none) |
| `--pin MODE` | cores for the single-thread phase: `auto` (default), `p`, `e`, `lpe`, `all` (a separate phase on each core type), `none` or a CPU list (`0,2,4-7`) |
| `--isa LIST` | multi-thread workload: `scalar` (default), `avx2`, `aes` or `all` — see [below](#instruction-sets-avx2fma-aes-ni) |
| `--repeat N` | run each phase N times; the report shows medians and the run-to-run spread |
| `--watts N` | use a fixed CPU wattage (no sensors available) |
| `--sweep W,W,...` | power sweep: run the phases at each power limit, e.g. `5,8,10,15,20,28,35,45` |
| `--sweep-mode MODE` | how to set the limit: `auto` (Linux powercap; Windows — frequency cap, multi-thread only) or `manual` (ThrottleStop / XTU) |
| `--settle N` | pause after each limit change, s (default 10) |
| `--out FILE` | report path (default `ppw-report-*.txt`) |
| `--list` | list detected sensors and exit |
| `--interactive` | open the interactive menu |
| `--version` | print the version and exit |

### Instruction sets (AVX2+FMA, AES-NI)

Instruction classes differ a lot in power: wide vector FMAs load a core the
most, but do several times more work per clock. Besides the general-purpose
workload there are profiles that stress specific core units:

| Profile | What it stresses | Rate unit |
|---------|------------------|-----------|
| `scalar` | general integer + FP code (default workload) | units/s |
| `avx2` | 256-bit FMA (FP32): science, rendering, codecs | GFLOPS (1 unit = 192 FLOP) |
| `aes` | AES-128 encryption, 8 blocks in parallel: disks, VPN, archives | GB/s (1 unit = 128 bytes) |

```bat
ppw-bench.exe --isa all --no-st --duration 30
```

* PPW is reported in the profile's units — **GFLOPS per W**, **GB/s per W** —
  physical quantities comparable with other benchmarks.
* Profiles run as multi-thread phases; single-thread phases are always `scalar`.
* bench detects support itself (CPUID); an unsupported profile is skipped
  with a warning. See `bench --caps`.
* Menu: "Наборы инструкций" in step 1. Works with the power sweep too (a
  GFLOPS/W-vs-limit curve). The Windows auto mode calibrates the frequency cap
  on the scalar workload, so AVX2 power can deviate from the step; such rows
  are marked `!`.
* On hybrid CPUs AES-NI throughput fluctuates noticeably (±15 %): the scheduler
  and Thread Director move such threads between cores. That is platform
  behaviour, not measurement error.
* **Workload version 2** (since ppw-bench 1.8): the scalar FP chain no longer
  overflows to `inf`, per-thread counters are cache-line aligned. Version 1
  and 2 "units/s" scores are not comparable — the version is in the report.

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
  interactive menu the "whole CPU" and "single-thread only" items work this way.

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

### Power sweep

PPW depends heavily on the power limit: at 10 W a CPU is usually far more
efficient than at 45 W. `--sweep` runs the selected phases at each limit
step (PL1 = PL2, no boost) and prints a "limit × phase" table.

```bash
sudo ./ppw-bench --sweep 5,8,10,15,20,28,35,45 --duration 30 --no-st
```

* **Linux (Intel)** — limits are set automatically through powercap
  (`intel-rapl` and `intel-rapl-mmio` when both exist). Requires root. The
  original limits are saved and restored afterwards — also on errors, Ctrl+C
  and a closed terminal.
* **Windows, auto mode** (default) — there is no public API for PL1, so the
  limit is **emulated with the power plan's frequency cap**
  (`PROCFREQMAX` / `PROCFREQMAX1`). At each step the app runs a load and,
  within 10–15 s, finds the cap at which package power equals the step. The
  CPU lowers both frequency and voltage, as with a real PL1, but the frequency
  is set by the cap rather than by firmware; the report says so.
  Limitations:
  * **multi-thread only** — a single-thread load is not limited by the cap,
    so single-thread phases are left out of the sweep;
  * power has a floor (~14 W on a Core Ultra 9 185H): lower steps are marked
    "skipped";
  * needs the package RAPL sensor (Energy Meter); no admin rights required.
  The original power plan values are restored afterwards — also when the
  console window is closed. If the process dies outright (power loss), the
  next run restores them: the originals are kept in
  `%LOCALAPPDATA%\ppw-bench\freqcap-restore.json`.
* **Windows, manual mode** (`--sweep-mode manual` or step 4 of the menu) —
  before each step the app asks you to set the limit in ThrottleStop or
  Intel XTU and press Enter. Use it for single-thread and below the floor.
* **AMD** — not supported yet (limits are set via `ryzenadj`).
* If the average package power of a step exceeds the limit by more than 10 %,
  the row is marked `!`: the limit was not actually enforced (BIOS lock, EC,
  thermald, TLP, …). If another program changed the limit during a step, it
  is listed under warnings.
* After every limit change there is a `--settle` pause (10 s). In the
  interactive menu this is step 3, which also shows the total run time.
* A `.csv` with the step table is saved next to the `.txt` — handy for a
  PPW-vs-power chart.

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
* **Core frequency** ("Частота ядра / ядер") — the real core clock under
  load. At the end of each phase, without letting the cores idle, bench runs
  a chain of dependent adds (exactly 1 cycle each) for ~0.25 s, so adds per
  second is the clock. OS counters are not used: on hybrid CPUs Windows reports
  values that are clearly too low. Single-thread phases show the clock and the
  CPU number; multi-thread phases show the average, split into P / E / LP-E on
  hybrid CPUs. Shows whether the power plan keeps cores at a low clock.
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
│   ├── report.py           # report (+ power sweep CSV)
│   ├── powerlimit.py       # power limits for --sweep
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
