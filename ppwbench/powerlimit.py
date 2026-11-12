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

Windows: штатного API для PL1 нет. Два варианта:
  * FreqCapLimiter (авто) — эмуляция лимита: потолок частоты плана
    электропитания (PROCFREQMAX / PROCFREQMAX1) подбирается регулятором под
    нагрузкой, пока мощность пакета не станет равна ступени. Процессор при
    этом снижает и частоту, и напряжение — как при PL1. Работает только для
    MT: однопоточную нагрузку потолок на практике не ограничивает.
  * ManualLimiter — лимит выставляет пользователь (ThrottleStop / Intel XTU),
    а программа ждёт Enter.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

POWERCAP = Path("/sys/class/powercap")
# имена ограничений powercap -> обозначение у Intel
CONSTRAINTS = {"long_term": "PL1", "short_term": "PL2"}
DEFAULT_STEPS = (5.0, 8.0, 10.0, 15.0, 20.0, 28.0, 35.0, 45.0)
MIN_W, MAX_W = 3.0, 250.0


class LimitError(RuntimeError):
    """Лимит не удалось выставить или он не принят: замер останавливается."""


class StepSkipped(LimitError):
    """Эта ступень недостижима (например, ниже минимальной мощности) — пропускаем её."""


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
    mt_only = False

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
    mt_only = False
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


# --------------------------------------------------------------------------
#  Windows: эмуляция лимита потолком частоты
# --------------------------------------------------------------------------

FREQ_SETTINGS = ("PROCFREQMAX", "PROCFREQMAX1")  # класс эффективности 0 и 1 (E и P)
MIN_CAP, MAX_CAP = 800, 6000  # МГц; 0 = без ограничения
CAP_STEP = 50
CAL_TOL = 0.04       # ступень найдена, если мощность в пределах ±4 %
CAL_SKIP_TOL = 0.10  # на минимальном потолке мощность выше цели на 10 % -> ступень недостижима
CAL_ITERS = 10
# P ~ f^0.8 в рабочем диапазоне (замер на Core Ultra 9 185H): шаг регулятора
# cap_new = cap * (target / P) ^ (1 / 0.8)
CAL_GAIN = 1.25


def _quantize(mhz: float) -> int:
    return int(min(MAX_CAP, max(MIN_CAP, round(mhz / CAP_STEP) * CAP_STEP)))


def initial_cap(target: float, history: list[tuple[int, float]]) -> int:
    """Стартовый потолок для ступени по уже найденным точкам (потолок, Вт)."""
    pts = sorted((c, p) for c, p in history if c > 0 and p > 0)
    if not pts:
        return 2000
    # ближайшая точка + та же степенная модель
    c, p = min(pts, key=lambda cp: abs(cp[1] - target))
    return _quantize(c * (target / p) ** CAL_GAIN)


def calibrate(target: float, set_cap, measure, start_cap: int,
              max_power: float | None = None) -> tuple[int, float, str]:
    """Подбирает потолок частоты, при котором мощность пакета ≈ target.

    set_cap(МГц) выставляет потолок (0 — без ограничения), measure() ждёт
    и возвращает среднюю мощность пакета, Вт. max_power — мощность без
    потолка, если уже известна. Возвращает (потолок, мощность, статус):
    "ok" — найдено; "above_max" — цель выше мощности без ограничения
    (потолок 0); "below_min" — даже на минимальном потолке мощность выше цели.

    Мощность без потолка гуляет (схема питания, EC, нагрев), поэтому
    max_power — только подсказка: «потолок не нужен» засчитывается лишь
    после замера без потолка прямо сейчас."""
    if max_power is not None and target >= max_power * (1 - CAL_TOL):
        set_cap(0)
        p0 = measure()
        if p0 is not None and p0 < target * (1 + CAL_TOL):
            return 0, p0, "above_max"
        # максимум был занижен — ищем потолок как обычно
    cap = _quantize(start_cap)
    best: tuple[float, int, float] | None = None
    for _ in range(CAL_ITERS):
        set_cap(cap)
        p = measure()
        if p is None or p <= 0:
            raise LimitError("нет данных о мощности пакета (RAPL) для подбора потолка")
        err = abs(p / target - 1)
        if best is None or err < best[0]:
            best = (err, cap, p)
        if err <= CAL_TOL:
            return cap, p, "ok"
        if p > target and cap <= MIN_CAP:
            return (cap, p, "below_min") if p > target * (1 + CAL_SKIP_TOL) else (cap, p, "ok")
        if p < target and cap >= MAX_CAP:
            set_cap(0)
            return 0, measure(), "above_max"
        nxt = _quantize(cap * (target / p) ** CAL_GAIN)
        if nxt == cap:  # упёрлись в шаг квантования
            break
        cap = nxt
    _err, cap, p = best  # type: ignore[misc]
    set_cap(cap)
    return cap, p, "ok"


def _powercfg(*args: str) -> str:
    r = subprocess.run(["powercfg", *args], capture_output=True, timeout=30)
    out = r.stdout.decode("cp866", "replace")
    if r.returncode != 0:
        raise LimitError(f"powercfg {' '.join(args)}: код {r.returncode}")
    return out


def _active_scheme(run=_powercfg) -> str:
    """GUID активной схемы электропитания."""
    m = re.search(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", run("/getactivescheme"))
    if not m:
        raise LimitError("powercfg не вернул GUID активной схемы")
    return m.group(0).lower()


def read_freq_caps(run=_powercfg, scheme: str = "SCHEME_CURRENT") -> dict[str, tuple[int, int]]:
    """{"PROCFREQMAX": (AC, DC), ...} схемы scheme, МГц (0 — без ограничения).

    Параметры скрытые, поэтому /qh. Текст ответа зависит от языка Windows,
    но значения AC и DC — всегда два последних числа 0x........ в нём."""
    caps = {}
    for name in FREQ_SETTINGS:
        vals = re.findall(r"0x[0-9a-fA-F]{8}", run("/qh", scheme, "SUB_PROCESSOR", name))
        if len(vals) < 2:
            raise LimitError(f"powercfg не вернул значения {name}")
        caps[name] = (int(vals[-2], 16), int(vals[-1], 16))
    return caps


# Ограничения ядер в схеме электропитания. Суффикс 1 — класс эффективности 1
# (на гибридных Intel это P-ядра), без суффикса — класс 0 (E и LP-E, а на
# обычных CPU — все ядра).
PLAN_LIMIT_SETTINGS = ("PROCTHROTTLEMAX", "PROCTHROTTLEMAX1", "PROCFREQMAX", "PROCFREQMAX1")


def read_plan_settings(names=PLAN_LIMIT_SETTINGS, run=_powercfg,
                       scheme: str = "SCHEME_CURRENT") -> dict[str, tuple[int, int]]:
    """{имя: (AC, DC)} для тех параметров, что есть в схеме (остальные пропускаются)."""
    out = {}
    for name in names:
        try:
            vals = re.findall(r"0x[0-9a-fA-F]{8}", run("/qh", scheme, "SUB_PROCESSOR", name))
        except (LimitError, OSError, subprocess.SubprocessError):
            continue
        if len(vals) >= 2:
            out[name] = (int(vals[-2], 16), int(vals[-1], 16))
    return out


def plan_limit_warnings(values: dict[str, tuple[int, int]], on_ac: bool,
                        hybrid: bool) -> list[str]:
    """Предупреждения, если схема питания урезает ядра для текущего источника питания.

    Например, OEM-утилиты ставят «максимальное состояние» E-ядер 25 % от сети:
    E- и LP-E-ядра тогда работают на ~1 ГГц, и все их результаты занижены."""
    idx, src, verb = (0, "от сети", "ac") if on_ac else (1, "от батареи", "dc")
    groups = [("", "E- и LP-E-ядра" if hybrid else "ядра")]
    if hybrid:
        groups.append(("1", "P-ядра"))
    warns = []
    for suffix, who in groups:
        throttle = values.get(f"PROCTHROTTLEMAX{suffix}", (100, 100))[idx]
        freq = values.get(f"PROCFREQMAX{suffix}", (0, 0))[idx]
        limits, fixes = [], []
        if throttle < 100:
            limits.append(f"максимальное состояние {throttle} %")
            fixes.append(f"PROCTHROTTLEMAX{suffix} 100")
        if freq > 0:
            limits.append(f"максимальная частота {freq} МГц")
            fixes.append(f"PROCFREQMAX{suffix} 0")
        if limits:
            cmd = " && ".join(f"powercfg /set{verb}valueindex SCHEME_CURRENT SUB_PROCESSOR {f}"
                              for f in fixes)
            warns.append(f"Схема питания урезает {who} ({src}): {', '.join(limits)} — их "
                         f"результаты будут занижены. Вернуть: {cmd} && "
                         "powercfg /setactive SCHEME_CURRENT")
    return warns


def _default_journal() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "ppw-bench" / "freqcap-restore.json"


def _write_caps(run, scheme: str, values: dict[str, tuple[int, int]]) -> None:
    for name, (ac, dc) in values.items():
        run("/setacvalueindex", scheme, "SUB_PROCESSOR", name, str(ac))
        run("/setdcvalueindex", scheme, "SUB_PROCESSOR", name, str(dc))
    # применяем, только если схема по-прежнему активна: /setactive на другую
    # схему переключил бы пользователю план электропитания
    if _active_scheme(run) == scheme:
        run("/setactive", scheme)


def recover_pending(run=_powercfg, journal: Path | None = None) -> list[str]:
    """Если прошлый замер прервался (закрыли окно, пропало питание), возвращает
    исходные потолки частоты из журнала. Возвращает сообщения для пользователя.

    Возвращаем, только если в схеме всё ещё стоит потолок, выставленный нами:
    если пользователь уже поменял настройки сам, их не трогаем."""
    journal = journal or _default_journal()
    if not journal.exists():
        return []
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
        scheme = data["scheme"]
        original = {k: (int(v[0]), int(v[1])) for k, v in data["values"].items()}
        applied = data.get("applied")
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        journal.unlink(missing_ok=True)
        return ["Найден повреждённый журнал прерванного замера — удалён."]

    def fmt(vals: dict[str, tuple[int, int]]) -> str:
        return ", ".join(f"{k} сеть {ac or 'без огр.'} / батарея {dc or 'без огр.'}"
                         for k, (ac, dc) in vals.items())

    try:
        current = read_freq_caps(run, scheme)
        ours = applied is None or all(ac == applied and dc == applied
                                      for ac, dc in current.values())
        if ours:
            _write_caps(run, scheme, original)
            msg = ("Прошлый ступенчатый замер прервался — исходные потолки частоты "
                   f"возвращены ({fmt(original)}).")
        else:
            msg = ("Прошлый ступенчатый замер прервался, но потолки частоты с тех пор "
                   f"изменены — оставляю как есть. Исходные были: {fmt(original)}.")
    except (LimitError, OSError, subprocess.SubprocessError) as exc:
        return [f"Прошлый замер прервался, а вернуть потолки частоты не удалось ({exc}). "
                f"Исходные: {fmt(original)} — верните их вручную (powercfg) или "
                "запустите ppw-bench ещё раз."]
    journal.unlink(missing_ok=True)
    return [msg]


class _ConsoleGuard:
    """Windows: при закрытии окна консоли, Ctrl+Break, выходе из системы
    процесс завершается без finally-блоков. Обработчик консоли успевает
    (у него ~5 с) вернуть настройки прямо в своём потоке."""

    EVENTS = {1, 2, 5, 6}  # CTRL_BREAK, CTRL_CLOSE, CTRL_LOGOFF, CTRL_SHUTDOWN

    def __init__(self, restore) -> None:
        self._restore = restore
        self._cb = None

    def handle(self, ctrl_type: int) -> bool:
        if ctrl_type in self.EVENTS:
            try:
                self._restore()
            except Exception:
                pass
        return False  # дальше — обработчик по умолчанию (завершение процесса)

    def install(self) -> None:
        if os.name != "nt" or self._cb is not None:
            return
        import ctypes
        from ctypes import wintypes
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        self._cb = proto(lambda t: bool(self.handle(int(t))))
        ctypes.windll.kernel32.SetConsoleCtrlHandler(self._cb, True)

    def remove(self) -> None:
        if self._cb is None:
            return
        import ctypes
        ctypes.windll.kernel32.SetConsoleCtrlHandler(self._cb, False)
        self._cb = None


class FreqCapLimiter:
    """Windows: эмуляция PL1 потолком частоты, подобранным под нагрузкой."""

    mode = "freqcap"
    mt_only = True
    SETTLE_S = 1.0    # после смены потолка
    step_overhead_s = 15.0  # примерное время подбора потолка на ступень
    MEASURE_S = 1.5   # окно усреднения мощности
    WARMUP_S = 2.0    # прогрев нагрузки перед первым замером максимума

    def __init__(self, reader=None, run=_powercfg, journal: Path | None = None) -> None:
        self.reader = reader
        self._run = run
        self.journal = journal or _default_journal()
        self._scheme: str | None = None
        self._saved: dict[str, tuple[int, int]] = {}
        self._applied: int | None = None
        self._lock = threading.Lock()
        self._guard = _ConsoleGuard(self.restore)
        self.history: list[tuple[int, float]] = []
        self.max_power: float | None = None
        self.last_note: str | None = None
        self._exe = None
        self._sampler = None
        self._threads = 0
        self._problem: str | None = None
        try:
            read_freq_caps(run)
            _active_scheme(run)
        except (LimitError, OSError, subprocess.SubprocessError) as exc:
            self._problem = f"не удалось прочитать настройки плана электропитания ({exc})"

    def problem(self) -> str | None:
        if self._problem:
            return self._problem
        if self.reader is not None and self.reader.kind() != "pkg":
            return "для подбора лимита нужен датчик мощности пакета (RAPL / Energy Meter)"
        return None

    def describe(self) -> str:
        return ("автоматически: эмуляция PL1 потолком частоты Windows "
                "(PROCFREQMAX), подбор под нагрузкой; только многопоток")

    def attach(self, exe, sampler, threads: int) -> None:
        """Нагрузка и датчики для подбора потолка (известны только после сборки bench)."""
        self._exe, self._sampler, self._threads = exe, sampler, threads

    def _write_journal(self) -> None:
        """Журнал на диске: при обрыве процесса его подхватит recover_pending()."""
        try:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            self.journal.write_text(json.dumps({
                "scheme": self._scheme,
                "values": {k: list(v) for k, v in self._saved.items()},
                "applied": self._applied,
            }), encoding="utf-8")
        except OSError:
            pass  # без журнала остаётся finally и обработчик консоли

    def save(self) -> None:
        self._scheme = _active_scheme(self._run)
        self._saved = read_freq_caps(self._run, self._scheme)
        self._write_journal()
        self._guard.install()

    def saved_lines(self) -> list[str]:
        def fmt(v: int) -> str:
            return "без ограничения" if v == 0 else f"{v} МГц"
        return [f"{name}: сеть {fmt(ac)}, батарея {fmt(dc)}"
                for name, (ac, dc) in self._saved.items()]

    def _set(self, mhz: int) -> None:
        scheme = self._scheme or "SCHEME_CURRENT"
        with self._lock:
            if self._saved:
                self._applied = mhz
                self._write_journal()  # до записи: обрыв посередине тоже подхватится
            for name in FREQ_SETTINGS:
                for idx in ("/setacvalueindex", "/setdcvalueindex"):
                    self._run(idx, scheme, "SUB_PROCESSOR", name, str(mhz))
            self._run("/setactive", scheme)
            self._applied = mhz

    def _measure(self) -> float | None:
        from ppwbench import core  # здесь, чтобы powerlimit не тянул core при импорте
        time.sleep(self.SETTLE_S)
        t0 = time.perf_counter()
        time.sleep(self.MEASURE_S)
        window = [s for s in self._sampler.samples if s["t"] >= t0]
        return core.window_power(window, "pkg")

    def apply(self, watts: float) -> float:
        if self._exe is None or self._sampler is None:
            raise LimitError("подбор потолка не подключён к нагрузке (attach)")
        print(f"     подбор потолка частоты под {watts:g} Вт ...", flush=True)
        proc = subprocess.Popen([str(self._exe), str(self._threads), "600"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if self.max_power is None:
                self._set(0)
                time.sleep(self.WARMUP_S)  # нагрузка только что стартовала
                self.max_power = self._measure()
            cap, power, status = calibrate(watts, self._set, self._measure,
                                           initial_cap(watts, self.history), self.max_power)
        finally:
            proc.kill()
            proc.wait()
        if cap > 0:
            self.history.append((cap, power))
        elif power:
            self.max_power = power  # свежий замер без потолка
        if status == "below_min":
            self._set(0)  # не держим минимальный потолок до следующей ступени
            raise StepSkipped(f"ниже минимума: даже на {cap} МГц пакет берёт {power:.1f} Вт")
        if status == "above_max":
            self.last_note = f"без потолка (максимум ~{power:.1f} Вт)"
        else:
            self.last_note = f"потолок {cap} МГц (~{power:.1f} Вт при подборе)"
        print(f"     {self.last_note}", flush=True)
        return watts

    def verify(self) -> list[str]:
        if self._applied is None:
            return []
        try:
            if self._scheme and _active_scheme(self._run) != self._scheme:
                return ["схема электропитания сменилась во время ступени — "
                        "потолок частоты к ней не применялся"]
            caps = read_freq_caps(self._run, self._scheme or "SCHEME_CURRENT")
        except LimitError as exc:
            return [str(exc)]
        return [f"{name}: {self._applied} → {ac} МГц"
                for name, (ac, _dc) in caps.items() if ac != self._applied]

    def restore(self) -> list[str]:
        """Возвращает исходные значения в ту же схему. Повторный вызов — no-op
        (его может сделать и обработчик консоли, и finally)."""
        with self._lock:
            if not self._saved:
                return []
            try:
                _write_caps(self._run, self._scheme or "SCHEME_CURRENT", self._saved)
            except (LimitError, OSError, subprocess.SubprocessError) as exc:
                return [str(exc)]  # журнал остаётся: вернём при следующем запуске
            self._saved = {}
            self._applied = None
            self.journal.unlink(missing_ok=True)
        self._guard.remove()
        return []


def limit_held(target_w: float, power_w: float | None) -> bool | None:
    """Соблюдён ли лимит: средняя мощность пакета не выше цели (+10 % и 0.5 Вт допуска)."""
    if power_w is None:
        return None
    return power_w <= target_w * 1.10 + 0.5


SWEEP_MODES = ("auto", "manual")


def for_backend(backend, reader=None, mode: str = "auto"):
    """Ограничитель для платформы: auto — Linux powercap или Windows потолок
    частоты; manual — пользователь выставляет лимит сам (ThrottleStop / XTU)."""
    if mode == "manual":
        return ManualLimiter()
    if backend.NAME == "Linux":
        return PowercapLimiter(backend.SYSFS / "class/powercap", backend.cpu_vendor())
    return FreqCapLimiter(reader)
