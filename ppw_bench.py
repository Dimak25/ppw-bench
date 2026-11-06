# -*- coding: utf-8 -*-
"""
ppw_bench.py - измерение Performance-per-Watt (PPW) для CPU.

Работает так:
  1) собирает и запускает bench.exe (C, Win32-потоки) - детерминированная
     нагрузка "целые + плавающая точка";
  2) параллельно снимает мощность CPU через счётчики PDH:
        \\Energy Meter(*)\\Power      -> RAPL: мощность пакета (rapl_package0_pkg)
        \\Power Meter(*)\\Power       -> мощность всей системы (батарея, мВт)
        \\Processor Information(_Total)\\% Processor Performance -> эфф. частота
  3) считает PPW = (работа/сек) / (средняя мощность, Вт) в установившемся окне;
  4) сохраняет отчёт в .txt.

Запуск:
    py ppw_bench.py                      # 60 с на фазу, MT + ST
    py ppw_bench.py --quick              # 20 с на фазу
    py ppw_bench.py --duration 120       # длиннее = стабильнее (PL1)
    py ppw_bench.py --threads 8 --no-st  # только MT на 8 потоках
    py ppw_bench.py --watts 45           # если датчиков нет: задать мощность вручную
    py ppw_bench.py --list               # показать найденные датчики и выйти
"""

from __future__ import annotations

import argparse
import ctypes
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import winreg
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH_C = HERE / "bench.c"
BENCH_EXE = HERE / ("bench.exe" if os.name == "nt" else "bench")

# --------------------------------------------------------------------------
#  Консоль / утилиты
# --------------------------------------------------------------------------


def setup_console() -> None:
    if os.name != "nt":
        return
    try:
        k32 = ctypes.windll.kernel32
        k32.SetConsoleOutputCP(65001)
        k32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def cpu_name() -> str:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        )
        val, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        return str(val).strip()
    except Exception:
        return platform.processor() or "unknown CPU"


def physical_cores() -> int | None:
    """Число физических ядер (GetLogicalProcessorInformationEx)."""
    if os.name != "nt":
        return None
    try:
        fn = ctypes.windll.kernel32.GetLogicalProcessorInformationEx
        fn.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        fn.restype = wintypes.BOOL
        relation_processor_core = 0

        length = wintypes.DWORD(0)
        fn(relation_processor_core, None, ctypes.byref(length))
        if length.value == 0:
            return None
        buf = ctypes.create_string_buffer(length.value)
        if not fn(relation_processor_core, buf, ctypes.byref(length)):
            return None
        data = buf.raw[: length.value]
        offset = 0
        count = 0
        while offset + 8 <= len(data):
            _rel, size = struct.unpack_from("<II", data, offset)
            if size < 8:
                break
            count += 1
            offset += size
        return count or None
    except Exception:
        return None


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


def _power_status() -> SYSTEM_POWER_STATUS:
    st = SYSTEM_POWER_STATUS()
    try:
        ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st))
    except Exception:
        pass
    return st


def ac_line_status() -> int:
    """1 = от сети, 0 = от батареи, 255 = неизвестно."""
    return int(_power_status().ACLineStatus)


def battery_saver_on() -> bool:
    """Бит 0 SystemStatusFlag = включён режим экономии заряда."""
    return bool(_power_status().SystemStatusFlag & 0x1)


SCHEME_NAMES = {
    "381b4222-f694-41f0-9685-ff5bb260df2e": "Сбалансированная",
    "a1841308-3541-4fab-bc81-f71556f20b4a": "Экономия энергии",
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": "Высокая производительность",
    "e9a42b02-d5df-448d-aa00-03f14749eb61": "Максимальная производительность",
}


def active_power_scheme() -> tuple[str, str]:
    """Возвращает (guid, человекочитаемое имя)."""
    try:
        out = subprocess.run(
            ["powercfg", "/getactivescheme"],
            capture_output=True,
            timeout=6,
            creationflags=0x08000000,
        ).stdout
        m = re.search(
            rb"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
            out,
        )
        if m:
            guid = m.group(1).decode("ascii").lower()
            return guid, SCHEME_NAMES.get(guid, "другая схема")
    except Exception:
        pass
    return "", "неизвестно"


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


def _ft(v: FILETIME) -> int:
    return (v.dwHighDateTime << 32) | v.dwLowDateTime


def cpu_load_pct(sample: float = 1.0) -> float | None:
    """Средняя загрузка CPU за sample секунд (по GetSystemTimes)."""
    try:
        k32 = ctypes.windll.kernel32
        i1, k1, u1 = FILETIME(), FILETIME(), FILETIME()
        if not k32.GetSystemTimes(ctypes.byref(i1), ctypes.byref(k1), ctypes.byref(u1)):
            return None
        time.sleep(sample)
        i2, k2, u2 = FILETIME(), FILETIME(), FILETIME()
        if not k32.GetSystemTimes(ctypes.byref(i2), ctypes.byref(k2), ctypes.byref(u2)):
            return None
        idle = _ft(i2) - _ft(i1)
        total = (_ft(k2) - _ft(k1)) + (_ft(u2) - _ft(u1))
        if total <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (1.0 - idle / total)))
    except Exception:
        return None


def environment_warnings() -> list[str]:
    warns: list[str] = []
    if ac_line_status() == 0:
        warns.append(
            "Ноутбук работает от батареи: CPU ограничен по мощности, "
            "результат не отражает максимальную производительность. "
            "Для честного теста подключите блок питания."
        )
    guid, name = active_power_scheme()
    if guid == "a1841308-3541-4fab-bc81-f71556f20b4a":
        warns.append(
            "Активна схема питания «Экономия энергии»: занижены частоты и лимит "
            "мощности. Переключите на «Сбалансированная» или "
            "«Высокая производительность»."
        )
    if battery_saver_on():
        warns.append("Включён режим экономии заряда (Battery saver) — он режет производительность.")
    load = cpu_load_pct(1.0)
    if load is not None and load > 12.0:
        warns.append(
            f"Фоновая загрузка CPU перед тестом ~{load:.0f}%: "
            "закройте лишние программы для чистого замера."
        )
    return warns



def fmt_si(value: float, unit: str, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    a = abs(value)
    if a >= 1e9:
        return f"{value / 1e9:.{digits}f} G{unit}"
    if a >= 1e6:
        return f"{value / 1e6:.{digits}f} M{unit}"
    if a >= 1e3:
        return f"{value / 1e3:.{digits}f} k{unit}"
    return f"{value:.{digits}f} {unit}"


# --------------------------------------------------------------------------
#  PDH: чтение счётчиков производительности Windows
# --------------------------------------------------------------------------

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2

pdh = ctypes.WinDLL("pdh.dll")
PDH_HQUERY = ctypes.c_void_p
PDH_HCOUNTER = ctypes.c_void_p
PDH_STATUS = wintypes.LONG


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [
        ("szName", wintypes.LPWSTR),
        ("FmtValue", PDH_FMT_COUNTERVALUE),
    ]


pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(PDH_HQUERY)]
pdh.PdhOpenQueryW.restype = PDH_STATUS
pdh.PdhAddEnglishCounterW.argtypes = [
    PDH_HQUERY,
    wintypes.LPCWSTR,
    ctypes.c_size_t,
    ctypes.POINTER(PDH_HCOUNTER),
]
pdh.PdhAddEnglishCounterW.restype = PDH_STATUS
pdh.PdhCollectQueryData.argtypes = [PDH_HQUERY]
pdh.PdhCollectQueryData.restype = PDH_STATUS
pdh.PdhGetFormattedCounterArrayW.argtypes = [
    PDH_HCOUNTER,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W),
]
pdh.PdhGetFormattedCounterArrayW.restype = PDH_STATUS
pdh.PdhCloseQuery.argtypes = [PDH_HQUERY]
pdh.PdhCloseQuery.restype = PDH_STATUS

COUNTERS = {
    "rapl": (r"\Energy Meter(*)\Power", "RAPL: мощность пакета/ядер"),
    "meter": (r"\Power Meter(*)\Power", "мощность всей системы"),
    "perf": (r"\Processor Information(_Total)\% Processor Performance", "эфф. частота"),
}


def _read_array(handle) -> dict[str, float]:
    if not handle:
        return {}
    size = wintypes.DWORD(0)
    count = wintypes.DWORD(0)
    status = pdh.PdhGetFormattedCounterArrayW(
        handle, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None
    )
    if (status & 0xFFFFFFFF) != PDH_MORE_DATA:
        return {}
    if size.value == 0:
        return {}
    buf = ctypes.create_string_buffer(size.value)
    items = ctypes.cast(buf, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))
    status = pdh.PdhGetFormattedCounterArrayW(
        handle, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), items
    )
    if status != 0:
        return {}
    out: dict[str, float] = {}
    for i in range(count.value):
        item = items[i]
        if item.FmtValue.CStatus != 0:
            continue
        name = item.szName or ""
        out[str(name)] = float(item.FmtValue.doubleValue)
    return out


def to_watts(raw: float | None) -> float | None:
    """Драйверы RAPL/Power Meter на Windows обычно отдают мВт.
    Значения > 500 Вт в ваттах нереальны для CPU, поэтому это мВт."""
    if raw is None or raw <= 0:
        return None
    return raw / 1000.0 if raw > 500 else raw


class PowerReader:
    """Один счётчик PDH, объединяющий RAPL-пакет, систему и частоту."""

    def __init__(self) -> None:
        self.query = PDH_HQUERY()
        self.handles: dict[str, PDH_HCOUNTER] = {}
        self.available = False
        self._pkg_instances: list[str] = []
        self._pp0_instances: list[str] = []
        self._open()

    def _open(self) -> None:
        st = pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query))
        if st != 0:
            return
        for key, (path, _desc) in COUNTERS.items():
            h = PDH_HCOUNTER()
            st = pdh.PdhAddEnglishCounterW(self.query, path, 0, ctypes.byref(h))
            if st == 0:
                self.handles[key] = h
        self.available = bool(self.handles)

    def prime(self) -> None:
        """Первые два сбора нужны rate-счётчикам."""
        if not self.available:
            return
        pdh.PdhCollectQueryData(self.query)
        time.sleep(0.35)
        pdh.PdhCollectQueryData(self.query)
        self._discover()

    def _discover(self) -> None:
        rapl = _read_array(self.handles.get("rapl"))
        self._pkg_instances = sorted(k for k in rapl if "pkg" in k.lower())
        self._pp0_instances = sorted(k for k in rapl if "pp0" in k.lower())

    def read(self) -> dict[str, float | None]:
        if not self.available:
            return {}
        pdh.PdhCollectQueryData(self.query)
        rapl = _read_array(self.handles.get("rapl"))
        meter = _read_array(self.handles.get("meter"))
        perf = _read_array(self.handles.get("perf"))

        pkg = sum(rapl[k] for k in self._pkg_instances if k in rapl) if self._pkg_instances else None
        pp0 = sum(rapl[k] for k in self._pp0_instances if k in rapl) if self._pp0_instances else None
        sys_raw = next(iter(meter.values()), None) if meter else None
        perf_pct = next(iter(perf.values()), None) if perf else None

        return {
            "pkg": to_watts(pkg),
            "pp0": to_watts(pp0),
            "sys": to_watts(sys_raw),
            "perf_pct": perf_pct,
        }

    def describe(self) -> str:
        parts = []
        if any("pkg" in k.lower() for k in _read_array(self.handles.get("rapl"))):
            parts.append("RAPL (Energy Meter / rapl_package0_pkg)")
        if _read_array(self.handles.get("meter")):
            parts.append("Power Meter (система)")
        return ", ".join(parts) if parts else "датчики PDH не найдены"


class Sampler(threading.Thread):
    def __init__(self, reader: PowerReader, interval: float = 0.2) -> None:
        super().__init__(daemon=True)
        self.reader = reader
        self.interval = interval
        self.samples: list[dict[str, float]] = []
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            t = time.perf_counter()
            vals = self.reader.read()
            if vals:
                vals["t"] = t
                self.samples.append(vals)  # type: ignore[arg-type]
            if self._stop_event.wait(self.interval):
                break

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=2.0)


# --------------------------------------------------------------------------
#  Сборка C-бенчмарка
# --------------------------------------------------------------------------


def find_gcc() -> str | None:
    exe = shutil.which("gcc")
    if exe:
        return exe
    for cand in (
        r"C:\TDM-GCC-64\bin\gcc.exe",
        r"C:\msys64\mingw64\bin\gcc.exe",
        r"C:\mingw64\bin\gcc.exe",
        r"C:\Program Files\mingw64\bin\gcc.exe",
    ):
        if Path(cand).exists():
            return cand
    return None


def ensure_bench() -> Path:
    if not BENCH_C.exists():
        raise SystemExit(f"Не найден исходник: {BENCH_C}")
    if BENCH_EXE.exists() and BENCH_EXE.stat().st_mtime >= BENCH_C.stat().st_mtime:
        return BENCH_EXE
    gcc = find_gcc()
    if not gcc:
        raise SystemExit(
            "Не найден gcc. Установите MinGW-w64 / TDM-GCC (или добавьте gcc в PATH),\n"
            "после чего запустите скрипт снова."
        )
    print(f"Сборка benchmark: {gcc} -O2 -static bench.c -o bench.exe")
    cmd = [gcc, "-O2", "-static", str(BENCH_C), "-o", str(BENCH_EXE)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit("Ошибка сборки bench.c:\n" + (res.stderr or res.stdout))
    return BENCH_EXE


# --------------------------------------------------------------------------
#  Запуск фазы и расчёт
# --------------------------------------------------------------------------


def interp_work(points: list[tuple[float, float]], elapsed_ms: float) -> float:
    """Линейная интерполяция накопленной работы по времени (мс)."""
    if not points:
        return 0.0
    if elapsed_ms <= points[0][0]:
        return points[0][1]
    if elapsed_ms >= points[-1][0]:
        return points[-1][1]
    lo, hi = 0, len(points) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if points[mid][0] <= elapsed_ms:
            lo = mid
        else:
            hi = mid
    x0, y0 = points[lo]
    x1, y1 = points[hi]
    if x1 <= x0:
        return y1
    return y0 + (y1 - y0) * (elapsed_ms - x0) / (x1 - x0)


def run_phase(exe: Path, threads: int, duration: float, sampler: Sampler) -> dict:
    proc = subprocess.Popen(
        [str(exe), str(threads), f"{duration:.3f}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert proc.stdout is not None

    t_start = None
    t_end = None
    ticks: list[tuple[float, float]] = []  # (elapsed_ms, work)
    line_buf = b""
    try:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            line_buf += chunk
            while b"\n" in line_buf:
                raw, line_buf = line_buf.split(b"\n", 1)
                now = time.perf_counter()
                line = raw.decode("ascii", "replace").strip()
                if not line:
                    continue
                parts = line.split()
                if parts[0] == "START":
                    t_start = now
                elif parts[0] == "TICK" and len(parts) >= 3:
                    ticks.append((float(parts[1]), float(parts[2])))
                elif parts[0] == "DONE" and len(parts) >= 3:
                    t_end = now
                    ticks.append((float(parts[1]), float(parts[2])))
    finally:
        proc.wait(timeout=10)

    if t_start is None or t_end is None or t_end <= t_start:
        raise RuntimeError("бенчмарк не сообщил START/DONE")

    # окно установившегося режима: отбрасываем прогрев и хвост
    warmup = min(max(8.0, duration * 0.25), duration * 0.45)
    w_start = t_start + warmup
    w_end = t_end - 0.3
    if w_end <= w_start:
        w_start, w_end = t_start + duration * 0.3, t_end

    work0 = interp_work(ticks, (w_start - t_start) * 1000.0)
    work1 = interp_work(ticks, (w_end - t_start) * 1000.0)
    steady = w_end - w_start
    work_per_s = max(work1 - work0, 0.0) / steady if steady > 0 else 0.0
    total_work = ticks[-1][1] if ticks else 0.0

    # средние значения датчиков в том же окне
    window = [s for s in sampler.samples if w_start <= s["t"] <= w_end]

    def avg(key: str) -> float | None:
        vals = [s[key] for s in window if s.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    def cv(key: str) -> float | None:
        vals = [s[key] for s in window if s.get(key) is not None]
        if len(vals) < 3:
            return None
        m = sum(vals) / len(vals)
        if m <= 0:
            return None
        var = sum((v - m) ** 2 for v in vals) / len(vals)
        return (var ** 0.5) / m * 100.0

    pkg = avg("pkg")
    pp0 = avg("pp0")
    sysw = avg("sys")

    # разброс скорости по секундам внутри окна
    pts = [
        (t_start + e / 1000.0, w)
        for (e, w) in ticks
        if w_start <= t_start + e / 1000.0 <= w_end
    ]
    rates = [
        (wb - wa) / (tb - ta)
        for (ta, wa), (tb, wb) in zip(pts, pts[1:])
        if tb - ta > 0.05
    ]
    rate_cv = None
    if len(rates) >= 3:
        rm = sum(rates) / len(rates)
        if rm > 0:
            rate_cv = (sum((r - rm) ** 2 for r in rates) / len(rates)) ** 0.5 / rm * 100.0

    return {
        "threads": threads,
        "duration": duration,
        "steady": steady,
        "total_work": total_work,
        "work_per_s": work_per_s,
        "pkg_w": pkg,
        "pp0_w": pp0,
        "sys_w": sysw,
        "pkg_cv": cv("pkg"),
        "rate_cv": rate_cv,
        "energy_j": pkg * steady if pkg else None,
        "ppw_pkg": work_per_s / pkg if pkg else None,
        "ppw_sys": work_per_s / sysw if sysw else None,
        "samples": len(window),
        "time_s": datetime.now(),
    }


# --------------------------------------------------------------------------
#  Отчёт
# --------------------------------------------------------------------------


def build_report(meta: dict, phases: list[dict], source: str) -> str:
    L: list[str] = []
    bar = "=" * 66
    dash = "-" * 66
    L.append(bar)
    L.append("  PPW BENCH — производительность на ватт")
    L.append(bar)
    L.append(f"Дата              : {meta['date']}")
    L.append(f"Процессор         : {meta['cpu']}")
    L.append(f"Ядра / потоки     : {meta['cores']} / {meta['threads']}")
    L.append(f"ОС                : {meta['os']}")
    L.append(f"Питание           : {meta['power']}")
    L.append(f"Источник мощности : {source}")
    L.append(f"Длительность      : {meta['duration']:.0f} с на фазу (прогрев {meta['warmup']:.0f} с)")
    if meta.get("warnings"):
        L.append(dash)
        L.append("ВНИМАНИЕ")
        for w in meta["warnings"]:
            L.append(f"  ! {w}")
    L.append(dash)

    for i, ph in enumerate(phases, 1):
        kind = "MULTI-THREAD" if ph["threads"] > 1 else "SINGLE-THREAD"
        L.append(f"ФАЗА {i} — {kind} ({ph['threads']} поток(ов))")
        L.append(f"  Производительность : {fmt_si(ph['work_per_s'], 'ед/с')}")
        L.append(f"  Работа за тест      : {fmt_si(ph['total_work'], 'ед')}")
        if ph["pkg_w"]:
            L.append(f"  Ср. мощность CPU    : {ph['pkg_w']:.2f} Вт (пакет)")
        if ph["pp0_w"]:
            L.append(f"  Ср. мощность ядер   : {ph['pp0_w']:.2f} Вт (pp0)")
        if ph["sys_w"]:
            L.append(f"  Ср. мощность системы: {ph['sys_w']:.2f} Вт")
        if ph["energy_j"]:
            L.append(f"  Энергия (пакет)     : {ph['energy_j']:.0f} Дж")
        if ph["ppw_pkg"]:
            L.append(f"  PPW (пакет)         : {fmt_si(ph['ppw_pkg'], 'ед/с на Вт')}")
        if ph["ppw_sys"]:
            L.append(f"  PPW (система)       : {fmt_si(ph['ppw_sys'], 'ед/с на Вт')}")
        stab = []
        if ph["pkg_cv"] is not None:
            stab.append(f"мощность ±{ph['pkg_cv']:.1f}%")
        if ph["rate_cv"] is not None:
            stab.append(f"скорость ±{ph['rate_cv']:.1f}%")
        if stab:
            L.append(f"  Стабильность        : {', '.join(stab)}")
        L.append(f"  Окно измерения      : {ph['steady']:.1f} с, {ph['samples']} замеров")
        L.append(dash)

    L.append("ИТОГ")
    for ph in phases:
        kind = "MT" if ph["threads"] > 1 else "ST"
        if ph["ppw_pkg"]:
            L.append(f"  {kind}: {fmt_si(ph['ppw_pkg'], 'ед/с на Вт')} (по мощности пакета)")
        elif ph["ppw_sys"]:
            L.append(f"  {kind}: {fmt_si(ph['ppw_sys'], 'ед/с на Вт')} (по мощности системы)")
        else:
            L.append(f"  {kind}: PPW не измерен (нет датчика мощности)")
    L.append(bar)
    L.append("PPW = сколько единиц работы в секунду даёт каждый ватт.")
    L.append("Чем выше — тем эффективнее. Сравнивайте прогоны при одинаковой")
    L.append("длительности, питании от сети и фоновой нагрузке.")
    return "\n".join(L)


# --------------------------------------------------------------------------
#  main
# --------------------------------------------------------------------------


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(
        description="Тест Performance-per-Watt (PPW) для CPU",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--duration", type=float, default=60.0, help="секунд на фазу")
    ap.add_argument("--quick", action="store_true", help="быстрый режим: 20 с на фазу")
    ap.add_argument("--threads", type=int, default=0, help="потоков в MT-фазе (0 = все)")
    ap.add_argument("--no-st", action="store_true", help="не запускать single-thread фазу")
    ap.add_argument("--only-st", action="store_true", help="только single-thread фаза")
    ap.add_argument("--watts", type=float, default=None,
                    help="задать мощность CPU вручную (если датчики недоступны)")
    ap.add_argument("--out", type=str, default=None, help="путь к .txt-отчёту")
    ap.add_argument("--list", action="store_true", help="показать датчики и выйти")
    args = ap.parse_args()

    duration = 20.0 if args.quick else args.duration
    if duration < 8:
        ap.error("--duration должен быть >= 8 секунд")

    logical = os.cpu_count() or 1
    mt_threads = args.threads if args.threads > 0 else logical

    reader = PowerReader()
    reader.prime()

    if args.list:
        print("Доступные источники мощности:")
        print("  ", reader.describe())
        for key, (path, desc) in COUNTERS.items():
            vals = _read_array(reader.handles.get(key))
            print(f"  [{key}] {path}  ->  {desc}")
            for name, val in list(vals.items())[:12]:
                w = to_watts(val) if key in ("rapl", "meter") else None
                extra = f"  (~{w:.2f} Вт)" if w else ""
                print(f"        {name}: {val:.3f}{extra}")
        return 0

    exe = ensure_bench()
    exe = Path(exe)

    # источник мощности
    manual = args.watts
    source = ""
    if manual:
        source = f"вручную (--watts {manual:g} Вт)"
    else:
        desc = reader.describe()
        if "RAPL" in desc:
            source = "RAPL: мощность пакета CPU (Energy Meter)"
        elif "Power Meter" in desc:
            source = "Power Meter: мощность системы (RAPL недоступен)"
        else:
            source = "нет датчиков — PPW не будет посчитан"

    ac = ac_line_status()
    power_note = {
        0: "от батареи (для максимальной производительности подключите адаптер)",
        1: "от сети",
    }.get(ac, "неизвестно")

    warnings = environment_warnings()

    phases_spec: list[tuple[int, str]] = []
    if not args.only_st:
        phases_spec.append((mt_threads, "MT"))
    if not args.no_st:
        phases_spec.append((1, "ST"))

    if not phases_spec:
        phases_spec = [(mt_threads, "MT")]

    meta = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cpu": cpu_name(),
        "cores": physical_cores() or os.cpu_count(),
        "threads": logical,
        "os": platform.platform(),
        "power": power_note,
        "duration": duration,
        "warmup": min(max(8.0, duration * 0.25), duration * 0.45),
        "warnings": warnings,
    }

    warmup = meta["warmup"]
    total_time = (duration + 3) * len(phases_spec)
    print(f"CPU: {meta['cpu']}  ({meta['cores']} ядер / {meta['threads']} потоков)")
    print(f"Мощность: {source}")
    print(f"Питание: {power_note}")
    for w in warnings:
        print(f"  ! {w}")
    print(f"План: {len(phases_spec)} фаза(ы) по {duration:.0f} с (прогрев {warmup:.0f} с), ~{total_time:.0f} с всего")
    print()

    sampler = Sampler(reader)
    sampler.start()
    results: list[dict] = []
    try:
        for threads, tag in phases_spec:
            kind = "MULTI-THREAD" if threads > 1 else "SINGLE-THREAD"
            print(f"[{tag}] {kind}, {threads} поток(ов), {duration:.0f} с ...", flush=True)
            res = run_phase(exe, threads, duration, sampler)

            if manual:
                res["pkg_w"] = manual
                res["ppw_pkg"] = res["work_per_s"] / manual
                res["energy_j"] = manual * res["steady"]
            elif res["pkg_w"] is None and res["sys_w"] is not None:
                # RAPL нет — считаем PPW по мощности системы
                res["ppw_pkg"] = res["work_per_s"] / res["sys_w"]
                res["energy_j"] = res["sys_w"] * res["steady"]
            elif res["pkg_w"] is None and res["sys_w"] is None:
                res["ppw_pkg"] = None

            results.append(res)
            ppw = res["ppw_pkg"] or res["ppw_sys"]
            power = res["pkg_w"] or res["sys_w"]
            print(
                f"     {fmt_si(res['work_per_s'], 'ед/с')}"
                + (f"   {power:.1f} Вт" if power else "")
                + (f"   PPW {fmt_si(ppw, 'ед/с на Вт')}" if ppw else ""),
                flush=True,
            )
    finally:
        sampler.stop()

    report = build_report(meta, results, source)

    out = Path(args.out) if args.out else HERE / f"ppw-report-{datetime.now():%Y%m%d-%H%M%S}.txt"
    if out.suffix.lower() != ".txt":
        out = out.with_suffix(".txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report + "\n", encoding="utf-8-sig")

    print()
    print(report)
    print()
    print(f"Отчёт сохранён: {out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        raise SystemExit(130)
