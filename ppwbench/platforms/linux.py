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

import contextlib
import os
import platform
import subprocess
import time
from pathlib import Path

from ppwbench.cpusets import classify_tiers, format_cpulist, parse_cpulist

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


def cpu_vendor() -> str:
    for line in (_read(PROCFS / "cpuinfo") or "").splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip() == "vendor_id":
            return value.strip()
    return ""


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


def _llc_level(cpu: int) -> int | None:
    """Уровень кэша последнего уровня у CPU (3 = L3, 2 = только L2)."""
    best = None
    for idx in (SYSFS / f"devices/system/cpu/cpu{cpu}/cache").glob("index[0-9]*"):
        if _read(idx / "type") == "Instruction":
            continue
        lvl = _read_int(idx / "level")
        if lvl is not None and (best is None or lvl > best):
            best = lvl
    return best


def _lpe_candidates(classes: dict[int, float]) -> set[int]:
    """«Не-P» ядра с кэшем последнего уровня ниже, чем у P-ядер.

    У Meteor Lake LP-E-ядра на SoC-кристалле и не видят L3 (только L2),
    хотя PMU у них общий с E-ядрами (cpu_atom). У AMD Zen 5c L3 есть,
    поэтому они в кандидаты не попадают."""
    if not classes:
        return set()
    top = max(classes.values())
    levels = {c: _llc_level(c) for c in classes}
    p_levels = {lv for c, lv in levels.items() if classes[c] == top and lv is not None}
    if not p_levels:
        return set()
    p_max = max(p_levels)
    return {c for c, lv in levels.items()
            if classes[c] != top and lv is not None and lv < p_max}


def core_types() -> dict[str, list[int]] | None:
    """Типы ядер гибридного CPU: {"p": [...], "e": [...], ["lpe": [...]]} или None.

    Intel (Alder Lake и новее): PMU cpu_core / cpu_atom в /sys/devices;
    LP-E (Meteor Lake) выделяются из cpu_atom по отсутствию L3.
    Иначе (например, AMD Zen 5 + Zen 5c) — по максимальной частоте ядер:
    гибридным считаем CPU, где медленные ядра уступают быстрым больше 13 %.
    Небольшие различия (preferred cores у amd-pstate) гибридом не считаются."""
    core = _read(SYSFS / "devices/cpu_core/cpus")
    atom = _read(SYSFS / "devices/cpu_atom/cpus")
    if core and atom:
        try:
            p, e = parse_cpulist(core), parse_cpulist(atom)
        except ValueError:
            p, e = [], []
        if p and e:
            classes = {c: 1.0 for c in p}
            classes.update({c: 0.0 for c in e})
            return classify_tiers(classes, _lpe_candidates(classes))

    freqs: dict[int, float] = {}
    for d in (SYSFS / "devices/system/cpu").glob("cpu[0-9]*"):
        f = _read_int(d / "cpufreq/cpuinfo_max_freq")
        if f:
            freqs[int(d.name[3:])] = float(f)
    if len(freqs) < 2:
        return None
    top = max(freqs.values())
    if min(freqs.values()) / top > 0.87:
        return None
    classes = {c: (1.0 if f >= 0.9 * top else 0.0) for c, f in freqs.items()}
    return classify_tiers(classes, _lpe_candidates(classes))


@contextlib.contextmanager
def pinned(cpus: list[int] | None):
    """Процессы, запущенные внутри блока, работают только на cpus.

    В Linux аффинити наследуется от потока, который делает fork/exec:
    ограничиваем текущий поток, запускаем bench и возвращаем маску.
    Фоновый поток опроса датчиков это не затрагивает."""
    if not cpus:
        yield None
        return
    try:
        saved = os.sched_getaffinity(0)
    except (AttributeError, OSError):
        yield None
        return
    target = set(cpus) & saved
    if not target:
        yield None
        return
    os.sched_setaffinity(0, target)
    try:
        yield sorted(target)
    finally:
        os.sched_setaffinity(0, saved)


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


def capped_cpus() -> list[int]:
    """CPU, у которых верхняя граница частоты ниже аппаратного максимума
    (cpupower, TLP, auto-cpufreq и т.п.). Допуск 2 % на округления драйвера."""
    out = []
    for d in (SYSFS / "devices/system/cpu").glob("cpu[0-9]*"):
        hw = _read_int(d / "cpufreq/cpuinfo_max_freq")
        cur = _read_int(d / "cpufreq/scaling_max_freq")
        if hw and cur and cur < hw * 0.98:
            out.append(int(d.name[3:]))
    return sorted(out)


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
    capped = capped_cpus()
    if capped:
        warns.append(
            f"Максимальная частота урезана (scaling_max_freq < cpuinfo_max_freq) на CPU "
            f"{format_cpulist(capped)} — их результаты будут занижены. Вернуть: "
            "sudo cpupower frequency-set -u <макс. частота> или настройки TLP / auto-cpufreq."
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
    иначе мощность посчитается дважды. Тот же драйвер работает на AMD Zen.

    На AMD зона core берётся из MSR_AMD_CORE_ENERGY_STATUS, который считает
    энергию одного ядра (того, где ядро ОС его читает), а не всех ядер:
    получается ~1/N мощности ядер в MT и почти 0 Вт в ST. Такую зону не
    показываем, чтобы не вводить в заблуждение."""
    groups: dict[str, list[EnergyCounter]] = {"pkg": [], "pp0": [], "psys": []}
    per_core_only = cpu_vendor() in ("AuthenticAMD", "HygonGenuine")
    base = SYSFS / "class/powercap"
    for zone in sorted(base.glob("intel-rapl:*")):
        if not (zone / "energy_uj").exists():
            continue
        name = (_read(zone / "name") or "").lower()
        top_level = zone.name.count(":") == 1
        if top_level and name.startswith("package"):
            groups["pkg"].append(EnergyCounter(zone))
        elif not top_level and name == "core" and not per_core_only:
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
