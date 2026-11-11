# -*- coding: utf-8 -*-
"""Лимиты мощности CPU для ступенчатого замера (--sweep).

Linux (Intel): лимиты PL1/PL2 пишутся в powercap:
    /sys/class/powercap/intel-rapl:0/constraint_N_power_limit_uw
где constraint с именем long_term — это PL1, short_term — PL2. На многих
ноутбуках есть второй интерфейс intel-rapl-mmio:0 с собственными лимитами;
действует меньший из двух, поэтому пишем в оба. PL1 = PL2 = цель, чтобы
не было буста в начале ступени.

Исходные значения запоминаются в save() и обязательно возвращаются в
restore() — иначе после сбоя ноутбук останется, например, на 5 Вт.

Windows: штатного API для PL1 нет, поэтому лимит выставляет пользователь
(ThrottleStop / Intel XTU), а программа ждёт Enter (ManualLimiter).
"""

from __future__ import annotations

import os
from pathlib import Path

POWERCAP = Path("/sys/class/powercap")
# имена ограничений powercap -> обозначение у Intel
CONSTRAINTS = {"long_term": "PL1", "short_term": "PL2"}
DEFAULT_STEPS = (5.0, 8.0, 10.0, 15.0, 20.0, 28.0, 35.0, 45.0)
MIN_W, MAX_W = 3.0, 250.0


class LimitError(RuntimeError):
    """Лимит не удалось выставить или он не принят."""


def parse_steps(text: str) -> list[float]:
    """'5,8,10' -> [5.0, 8.0, 10.0]. Ступени в ваттах, без повторов."""
    steps: list[float] = []
    for part in text.replace(";", ",").split(","):
        part = part.strip().lower().removesuffix("w").removesuffix("вт").strip()
        if not part:
            continue
        try:
            w = float(part.replace(" ", ""))
        except ValueError:
            raise ValueError(f"не число: {part!r}") from None
        if not MIN_W <= w <= MAX_W:
            raise ValueError(f"{w:g} Вт вне диапазона {MIN_W:g}–{MAX_W:g} Вт")
        if w not in steps:
            steps.append(w)
    if not steps:
        raise ValueError("пустой список ступеней")
    return steps


def fmt_steps(steps) -> str:
    return ", ".join(f"{w:g}" for w in steps) + " Вт"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    raw = _read(path)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


class Constraint:
    """Одно ограничение зоны powercap (PL1 или PL2)."""

    def __init__(self, zone: Path, idx: int, pl: str) -> None:
        self.zone = zone
        self.pl = pl
        self.limit = zone / f"constraint_{idx}_power_limit_uw"
        self.max_uw = _read_int(zone / f"constraint_{idx}_max_power_uw") or 0

    @property
    def label(self) -> str:
        return f"{self.zone.name} {self.pl}"


class PowercapLimiter:
    """Автоматические лимиты через Linux powercap (Intel RAPL)."""

    mode = "auto"

    def __init__(self, root: Path = POWERCAP, vendor: str = "GenuineIntel") -> None:
        self.root = root
        self.vendor = vendor
        self.zones: list[Path] = []
        self.constraints: list[Constraint] = []
        for zone in sorted(root.glob("intel-rapl*:*")):
            if zone.name.count(":") != 1:  # подзоны core/uncore не трогаем
                continue
            if not (_read(zone / "name") or "").lower().startswith("package"):
                continue
            found = []
            for idx in range(8):
                name = _read(zone / f"constraint_{idx}_name")
                if name is None:
                    break
                if name in CONSTRAINTS and (zone / f"constraint_{idx}_power_limit_uw").exists():
                    found.append(Constraint(zone, idx, CONSTRAINTS[name]))
            if found:
                self.zones.append(zone)
                self.constraints += found
        self._saved: dict[Path, int] = {}
        self._applied: dict[Path, int] = {}

    def problem(self) -> str | None:
        """Почему автоматические лимиты недоступны (None — всё в порядке)."""
        if self.vendor in ("AuthenticAMD", "HygonGenuine"):
            return ("на AMD лимиты через powercap не задаются "
                    "(нужен ryzenadj — пока не поддерживается)")
        if not self.constraints:
            return "в powercap нет лимитов PL1/PL2 (intel-rapl)"
        if not all(os.access(c.limit, os.W_OK) for c in self.constraints):
            return "менять лимиты может только root: запустите через sudo"
        return None

    def describe(self) -> str:
        zones = ", ".join(z.name for z in self.zones)
        return f"автоматически через powercap ({zones}: PL1 = PL2 = ступень)"

    def save(self) -> None:
        self._saved = {}
        for c in self.constraints:
            val = _read_int(c.limit)
            if val is not None:
                self._saved[c.limit] = val
        for zone in self.zones:
            en = zone / "enabled"
            val = _read_int(en)
            if val is not None:
                self._saved[en] = val

    def saved_lines(self) -> list[str]:
        return [f"{c.label}: {self._saved[c.limit] / 1e6:g} Вт"
                for c in self.constraints if c.limit in self._saved]

    def apply(self, watts: float) -> float:
        """Ставит PL1 = PL2 = watts во всех зонах. Возвращает фактический лимит, Вт."""
        target = int(round(watts * 1e6))
        self._applied = {}
        effective = []
        for zone in self.zones:
            en = zone / "enabled"
            if _read_int(en) == 0:
                self._write(en, 1)
        for c in self.constraints:
            val = min(target, c.max_uw) if c.max_uw > 0 else target
            self._write(c.limit, val)
            got = _read_int(c.limit)
            if got is None or abs(got - val) > max(val // 100, 1000):
                raise LimitError(
                    f"{c.label}: записано {val / 1e6:g} Вт, а читается "
                    f"{'ничего' if got is None else f'{got / 1e6:g} Вт'} "
                    "(лимит заблокирован в BIOS?)"
                )
            self._applied[c.limit] = got
            effective.append(got)
        return min(effective) / 1e6

    def verify(self) -> list[str]:
        """Какие лимиты изменились после apply() (их перезаписал thermald, TLP и т.п.)."""
        changed = []
        for c in self.constraints:
            want = self._applied.get(c.limit)
            got = _read_int(c.limit)
            if want is not None and got != want:
                changed.append(f"{c.label}: {want / 1e6:g} → "
                               f"{'?' if got is None else f'{got / 1e6:g}'} Вт")
        return changed

    def restore(self) -> list[str]:
        """Возвращает исходные лимиты. Возвращает список ошибок (пустой — всё вернули)."""
        errors = []
        # сначала лимиты, потом enabled — пока зона включена, запись точно примется
        for path, val in sorted(self._saved.items(), key=lambda kv: kv[0].name == "enabled"):
            try:
                self._write(path, val)
            except LimitError as exc:
                errors.append(str(exc))
        return errors

    @staticmethod
    def _write(path: Path, value: int) -> None:
        try:
            path.write_text(f"{value}\n", encoding="ascii")
        except OSError as exc:
            raise LimitError(f"не удалось записать {path}: {exc}") from exc


class ManualLimiter:
    """Лимит выставляет пользователь (Windows: ThrottleStop / Intel XTU)."""

    mode = "manual"
    TOOLS = "ThrottleStop (TPL) или Intel XTU"

    def __init__(self, ask=None) -> None:
        self._ask = ask or (lambda prompt: input(prompt))

    def problem(self) -> str | None:
        return None

    def describe(self) -> str:
        return f"вручную ({self.TOOLS}: PL1 = PL2 = ступень)"

    def save(self) -> None:
        pass

    def saved_lines(self) -> list[str]:
        return []

    def apply(self, watts: float) -> float:
        print(f"  >>> Выставьте PL1 = PL2 = {watts:g} Вт в {self.TOOLS}.")
        try:
            self._ask("      Затем нажмите Enter... ")
        except EOFError:
            # stdin закрыт (NUL в Windows тоже считается консолью)
            raise LimitError("нет ответа с клавиатуры — ручному режиму нужна консоль") from None
        return watts

    def verify(self) -> list[str]:
        return []

    def restore(self) -> list[str]:
        print(f"  >>> Не забудьте вернуть прежние лимиты в {self.TOOLS}.")
        return []


def limit_held(target_w: float, power_w: float | None) -> bool | None:
    """Соблюдён ли лимит: средняя мощность пакета не выше цели (+10 % и 0.5 Вт допуска)."""
    if power_w is None:
        return None
    return power_w <= target_w * 1.10 + 0.5


def for_backend(backend):
    """Подходящий ограничитель для платформы."""
    if backend.NAME == "Linux":
        return PowercapLimiter(backend.SYSFS / "class/powercap", backend.cpu_vendor())
    return ManualLimiter()
