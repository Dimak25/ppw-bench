# -*- coding: utf-8 -*-
"""Linux: RAPL через powercap (/sys/class/powercap), /proc, /sys.

RAPL в Linux отдаёт не мощность, а накопленную энергию (energy_uj, мкДж).
Это даже лучше: энергия в окне замера считается разностью счётчиков,
а мощность — делением на время. Счётчик переполняется по max_energy_range_uj.

С ядра 5.10 energy_uj по умолчанию читает только root (CVE-2020-8694,
атака Platypus). Варианты: запуск через sudo или udev-правило
(packaging/linux/99-ppw-bench-powercap.rules), которое даёт право чтения
группе powercap.
"""

from __future__ import annotations

import os
import platform
import subprocess
import time
from pathlib import Path

NAME = "Linux"

# Корни вынесены в переменные, чтобы тесты могли подставить фейковый sysfs.
SYSFS = Path("/sys")
PROCFS = Path("/proc")

UDEV_HINT = (
    "Счётчики RAPL есть, но читать их может только root (так в ядре с 5.10). "
    "Запустите через sudo или один раз установите правило udev: "
    "sudo ./install-udev.sh (в архиве релиза; в репозитории — "
    "packaging/linux/install-udev.sh) — и перелогиньтесь."
)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    raw = _read(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------------
#  Сведения о системе
# --------------------------------------------------------------------------


def cpu_name() -> str:
    text = _read(PROCFS / "cpuinfo") or ""
    for key in ("model name", "Model", "Hardware", "cpu model"):
        for line in text.splitlines():
            name, sep, value = line.partition(":")
            if sep and name.strip() == key and value.strip():
                return value.strip()
    return platform.processor() or platform.machine() or "unknown CPU"


def physical_cores() -> int | None:
    """Уникальные пары (package, core) из /sys/devices/system/cpu/*/topology."""
    cores = set()
    for topo in (SYSFS / "devices/system/cpu").glob("cpu[0-9]*/topology"):
        pkg = _read(topo / "physical_package_id")
        core = _read(topo / "core_id")
        if pkg is not None and core is not None:
            cores.add((pkg, core))
    return len(cores) or None


def logical_cpus() -> int:
    """Учитывает taskset / cgroups, в отличие от os.cpu_count()."""
    try:
        return len(os.sched_getaffinity(0)) or 1
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def _power_supplies() -> list[tuple[Path, str]]:
    out = []
    for dev in sorted((SYSFS / "class/power_supply").glob("*")):
        kind = _read(dev / "type") or ""
        out.append((dev, kind))
    return out


def ac_line_status() -> int:
    """1 = от сети, 0 = от батареи, 255 = неизвестно."""
    mains_online = None
    has_battery = False
    for dev, kind in _power_supplies():
        if kind in ("Mains", "USB", "USB_C", "USB_PD"):
            online = _read_int(dev / "online")
            if online is not None:
                mains_online = bool(mains_online) or online == 1
        elif kind == "Battery":
            has_battery = True
            status = (_read(dev / "status") or "").lower()
            if status == "discharging":
                return 0
    if mains_online:
        return 1
    if mains_online is False and has_battery:
        return 0
    if not has_battery:
        return 1  # десктоп без батареи: питание от сети
    return 255


def battery_power_w() -> float | None:
    """Мощность разряда батареи, Вт (только когда ноутбук от батареи)."""
    for dev, kind in _power_supplies():
        if kind != "Battery":
            continue
        if (_read(dev / "status") or "").lower() != "discharging":
            continue
        p = _read_int(dev / "power_now")  # мкВт
        if p:
            return abs(p) / 1e6
        cur = _read_int(dev / "current_now")  # мкА
        volt = _read_int(dev / "voltage_now")  # мкВ
        if cur and volt:
            return abs(cur) * volt / 1e12
    return None


def _ppd_profile() -> str | None:
    """power-profiles-daemon / tuned-ppd: power-saver, balanced, performance."""
    try:
        res = subprocess.run(
            ["powerprofilesctl", "get"], capture_output=True, text=True, timeout=3
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = res.stdout.strip()
    return out if res.returncode == 0 and out else None


def _cpufreq(name: str) -> str | None:
    return _read(SYSFS / "devices/system/cpu/cpu0/cpufreq" / name)


def power_profile() -> str:
    parts = []
    ppd = _ppd_profile()
    if ppd:
        parts.append(f"профиль {ppd}")
    else:
        acpi = _read(SYSFS / "firmware/acpi/platform_profile")
        if acpi:
            parts.append(f"platform_profile {acpi}")
    driver = _cpufreq("scaling_driver")
    gov = _cpufreq("scaling_governor")
    if gov:
        parts.append(f"governor {gov}" + (f" ({driver})" if driver else ""))
    epp = _cpufreq("energy_performance_preference")
    if epp:
        parts.append(f"EPP {epp}")
    return ", ".join(parts) if parts else "неизвестно"


def _proc_stat_times() -> tuple[int, int] | None:
    line = (_read(PROCFS / "stat") or "").splitlines()[:1]
    if not line or not line[0].startswith("cpu "):
        return None
    vals = [int(v) for v in line[0].split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    total = sum(vals[:8])  # без guest/guest_nice: они уже входят в user
    return idle, total


def cpu_load_pct(sample: float = 1.0) -> float | None:
    a = _proc_stat_times()
    if a is None:
        return None
    time.sleep(sample)
    b = _proc_stat_times()
    if b is None:
        return None
    total = b[1] - a[1]
    if total <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - (b[0] - a[0]) / total)))


def environment_warnings() -> list[str]:
    warns: list[str] = []
    if ac_line_status() == 0:
        warns.append(
            "Ноутбук работает от батареи: CPU ограничен по мощности, "
            "результат не отражает максимальную производительность. "
            "Для честного теста подключите блок питания."
        )
    ppd = _ppd_profile()
    acpi = _read(SYSFS / "firmware/acpi/platform_profile")
    if ppd == "power-saver" or acpi in ("low-power", "quiet", "cool"):
        warns.append(
            "Активен энергосберегающий профиль питания: занижены частоты и лимит "
            "мощности. Переключите: powerprofilesctl set balanced (или performance)."
        )
    if _cpufreq("energy_performance_preference") == "power":
        warns.append(
            "EPP процессора = power: частоты режутся ради экономии. "
            "Для сравнимых результатов используйте balance_performance/default."
        )
    if _cpufreq("scaling_driver") == "acpi-cpufreq" and _cpufreq("scaling_governor") == "powersave":
        warns.append(
            "Governor powersave с драйвером acpi-cpufreq держит минимальную частоту: "
            "переключите на schedutil или performance."
        )
    load = cpu_load_pct(1.0)
    if load is not None and load > 12.0:
        warns.append(
            f"Фоновая загрузка CPU перед тестом ~{load:.0f}%: "
            "закройте лишние программы для чистого замера."
        )
    return warns


def compiler_candidates() -> list[str]:
    return ["/usr/bin/cc", "/usr/bin/gcc", "/usr/bin/clang", "/usr/local/bin/gcc"]


# --------------------------------------------------------------------------
#  RAPL через powercap
# --------------------------------------------------------------------------


class EnergyCounter:
    """Накопленная энергия зоны powercap с учётом переполнения счётчика."""

    def __init__(self, zone: Path) -> None:
        self.zone = zone
        self.name = _read(zone / "name") or zone.name
        self.max_uj = _read_int(zone / "max_energy_range_uj") or 0
        self._last_raw: int | None = None
        self._total_uj = 0
        self.error: str | None = None
        self.readable = self._probe()

    def _probe(self) -> bool:
        try:
            int((self.zone / "energy_uj").read_text())
            return True
        except PermissionError:
            self.error = "нет прав"
        except (OSError, ValueError) as exc:
            self.error = str(exc)
        return False

    def joules(self) -> float | None:
        """Энергия с первого вызова, Дж (монотонно растёт)."""
        try:
            raw = int((self.zone / "energy_uj").read_text())
        except (OSError, ValueError):
            return None
        if self._last_raw is not None:
            delta = raw - self._last_raw
            if delta < 0:  # переполнение
                delta += self.max_uj if self.max_uj else 0
                delta = max(delta, 0)
            self._total_uj += delta
        self._last_raw = raw
        return self._total_uj / 1e6


def discover_zones() -> dict[str, list[EnergyCounter]]:
    """pkg — пакеты (по сокетам), pp0 — ядра, psys — вся платформа.

    intel-rapl-mmio:* дублирует package-0 на части Intel — пропускаем,
    иначе мощность посчитается дважды. Тот же драйвер работает на AMD Zen."""
    groups: dict[str, list[EnergyCounter]] = {"pkg": [], "pp0": [], "psys": []}
    base = SYSFS / "class/powercap"
    for zone in sorted(base.glob("intel-rapl:*")):
        if not (zone / "energy_uj").exists():
            continue
        name = (_read(zone / "name") or "").lower()
        top_level = zone.name.count(":") == 1
        if top_level and name.startswith("package"):
            groups["pkg"].append(EnergyCounter(zone))
        elif not top_level and name == "core":
            groups["pp0"].append(EnergyCounter(zone))
        elif name == "psys":
            groups["psys"].append(EnergyCounter(zone))
    return groups


class PowerReader:
    def __init__(self) -> None:
        self.zones = discover_zones()
        self.available = True  # батарея может появиться и без RAPL
        self._prev: dict[str, tuple[float, float]] = {}

    def _readable(self, group: str) -> list[EnergyCounter]:
        return [z for z in self.zones[group] if z.readable]

    def _group_joules(self, group: str) -> float | None:
        zones = self._readable(group)
        if not zones:
            return None
        vals = [z.joules() for z in zones]
        if any(v is None for v in vals):
            return None
        return sum(vals)  # type: ignore[arg-type]

    def prime(self) -> None:
        self.read()
        time.sleep(0.35)

    def read(self) -> dict[str, float | None]:
        now = time.perf_counter()
        out: dict[str, float | None] = {}
        for group, key in (("pkg", "pkg"), ("pp0", "pp0"), ("psys", "sys")):
            j = self._group_joules(group)
            out[key] = None
            if j is None:
                continue
            out[key + "_j"] = j
            prev = self._prev.get(group)
            if prev and now > prev[0]:
                w = (j - prev[1]) / (now - prev[0])
                out[key] = w if w > 0 else None
            self._prev[group] = (now, j)
        if out.get("sys") is None and "sys_j" not in out:
            out["sys"] = battery_power_w()
        return out

    def kind(self) -> str | None:
        if self._readable("pkg"):
            return "pkg"
        if self._readable("psys") or battery_power_w() is not None:
            return "sys"
        return None

    def describe(self) -> str:
        parts = []
        if self._readable("pkg"):
            parts.append("RAPL (powercap: " + ", ".join(z.name for z in self._readable("pkg")) + ")")
        if self._readable("psys"):
            parts.append("RAPL psys (платформа)")
        elif battery_power_w() is not None:
            parts.append("батарея (power_supply, разряд)")
        if not parts:
            if any(self.zones.values()):
                return "RAPL найден, но нет прав на чтение (нужен sudo или udev-правило)"
            return "датчики мощности не найдены"
        return ", ".join(parts)

    def sensor_lines(self) -> list[str]:
        lines = [f"[powercap] {SYSFS / 'class/powercap'}"]
        vals = self.read()
        found = False
        for group, key in (("pkg", "pkg"), ("pp0", "pp0"), ("psys", "sys")):
            for z in self.zones[group]:
                found = True
                state = "ok" if z.readable else f"недоступен ({z.error})"
                lines.append(f"      {z.zone.name} [{z.name}] -> {group}: {state}")
            if vals.get(key) is not None and self._readable(group):
                lines.append(f"        ~{vals[key]:.2f} Вт")
        if not found:
            lines.append("      зон intel-rapl нет")
        bat = battery_power_w()
        lines.append("[power_supply] батарея: " + (f"~{bat:.2f} Вт (разряд)" if bat else "нет данных / не разряжается"))
        return lines

    def hints(self) -> list[str]:
        if any(self.zones.values()) and not self._readable("pkg"):
            if os.geteuid() != 0:
                return [UDEV_HINT]
        if not any(self.zones.values()):
            return [
                "Нет зон /sys/class/powercap/intel-rapl: попробуйте "
                "sudo modprobe intel_rapl_common (Intel, AMD Zen). В виртуальной "
                "машине RAPL обычно недоступен — используйте --watts N."
            ]
        return []
