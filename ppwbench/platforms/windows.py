# -*- coding: utf-8 -*-
"""Windows: PDH-счётчики (RAPL через Energy Meter), реестр, powercfg."""

from __future__ import annotations

import ctypes
import os
import platform
import re
import struct
import subprocess
import time
import winreg
from ctypes import wintypes

NAME = "Windows"
CREATE_NO_WINDOW = 0x08000000


# --------------------------------------------------------------------------
#  Сведения о системе
# --------------------------------------------------------------------------


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


def logical_cpus() -> int:
    return os.cpu_count() or 1


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
        st.ACLineStatus = 255
    return st


def ac_line_status() -> int:
    """1 = от сети, 0 = от батареи, 255 = неизвестно."""
    return int(_power_status().ACLineStatus)


def battery_saver_on() -> bool:
    """Бит 0 SystemStatusFlag = включён режим экономии заряда."""
    return bool(_power_status().SystemStatusFlag & 0x1)


POWER_SAVER_GUID = "a1841308-3541-4fab-bc81-f71556f20b4a"
SCHEME_NAMES = {
    "381b4222-f694-41f0-9685-ff5bb260df2e": "Сбалансированная",
    POWER_SAVER_GUID: "Экономия энергии",
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
            creationflags=CREATE_NO_WINDOW,
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


def power_profile() -> str:
    return active_power_scheme()[1]


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
    guid, _name = active_power_scheme()
    if guid == POWER_SAVER_GUID:
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


def compiler_candidates() -> list[str]:
    return [
        r"C:\TDM-GCC-64\bin\gcc.exe",
        r"C:\msys64\mingw64\bin\gcc.exe",
        r"C:\msys64\ucrt64\bin\gcc.exe",
        r"C:\mingw64\bin\gcc.exe",
        r"C:\Program Files\mingw64\bin\gcc.exe",
    ]


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
    """Счётчики \\Energy Meter\\Power и \\Power Meter\\Power отдают милливатты.

    Единицы фиксированы: раньше они угадывались по величине (> 500 -> мВт),
    из-за чего 300 мВт в простое превращались в «300 Вт»."""
    if raw is None or raw <= 0:
        return None
    return raw / 1000.0


class PowerReader:
    """Один запрос PDH, объединяющий RAPL-пакет, систему и частоту."""

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

    def kind(self) -> str | None:
        """"pkg" — есть RAPL пакета, "sys" — только мощность системы, None — ничего."""
        if any("pkg" in k.lower() for k in _read_array(self.handles.get("rapl"))):
            return "pkg"
        if _read_array(self.handles.get("meter")):
            return "sys"
        return None

    def describe(self) -> str:
        parts = []
        if any("pkg" in k.lower() for k in _read_array(self.handles.get("rapl"))):
            parts.append("RAPL (Energy Meter / rapl_package0_pkg)")
        if _read_array(self.handles.get("meter")):
            # драйвер батареи отдаёт мощность только при разряде
            parts.append("Power Meter (система, только от батареи)")
        return ", ".join(parts) if parts else "датчики PDH не найдены"

    def sensor_lines(self) -> list[str]:
        lines = []
        for key, (path, desc) in COUNTERS.items():
            vals = _read_array(self.handles.get(key))
            lines.append(f"[{key}] {path}  ->  {desc}")
            for name, val in list(vals.items())[:12]:
                w = to_watts(val) if key in ("rapl", "meter") else None
                extra = f"  (~{w:.2f} Вт)" if w else ""
                lines.append(f"      {name}: {val:.3f}{extra}")
        return lines

    def hints(self) -> list[str]:
        if self.kind() is None:
            return [
                "Нет провайдера Energy Meter: используйте --watts N "
                "(мощность CPU в ваттах) или внешний ваттметр."
            ]
        return []
