# -*- coding: utf-8 -*-
"""Командная строка и интерактивное меню.

    ppw-bench                       # меню (из консоли без аргументов)
    ppw-bench --quick               # 20 с на фазу
    ppw-bench --duration 120        # длиннее = стабильнее (PL1)
    ppw-bench --threads 8 --no-st   # только MT на 8 потоках
    ppw-bench --watts 45            # если датчиков нет: задать мощность вручную
    ppw-bench --list                # показать найденные датчики и выйти
    sudo ppw-bench --sweep 5,10,15,25 --duration 30   # PPW на разных лимитах (Linux)
    ppw-bench --isa all             # каждая фаза на каждом наборе: обычный, AVX2, AES
    ppw-bench --mt-pin all --no-st  # многопоток отдельно на P-, E- и LP-E-ядрах
"""

from __future__ import annotations

import argparse
import contextlib
import platform
import signal
import time
from datetime import datetime

from ppwbench import __version__, core, powerlimit, report, util
from ppwbench.cpusets import TIER_NAMES, TIER_ORDER, format_cpulist, parse_cpulist
from ppwbench.platforms import current as current_backend

DEFAULT_COOLDOWN = 10.0
DEFAULT_SETTLE = 10.0


def print_sensors(reader) -> None:
    print("Доступные источники мощности:")
    print("  ", reader.describe())
    for line in reader.sensor_lines():
        print("  ", line)
    for hint in reader.hints():
        print(f"   ! {hint}")


SUITES = {
    # ключ: (подпись, MT-фазы, ST-фазы)
    "cpu": ("Весь процессор", True, True),
    "mt": ("Только многопоток", True, False),
    "mttiers": ("Многопоток по ядрам", True, False),
    "st": ("Только однопоток", False, True),
}
# готовые варианты шага «наборы инструкций»: номер -> (подпись, профили)
ISA_PRESETS = {
    "1": ("Только обычный", ["scalar"]),
    "2": ("Все наборы", ["scalar", "avx2", "aes"]),
    "3": ("Только AVX2+FMA", ["avx2"]),
    "4": ("Только AES-NI", ["aes"]),
}
MENU_DURATIONS = (20.0, 60.0, 120.0)


def suite_phase_count(suite: str, tiers: dict[str, list[int]] | None,
                      profiles: int = 1) -> int:
    """Сколько фаз будет в наборе: MT (на всех ядрах или по одной на тип ядер)
    + по одной ST на каждый тип ядер — и всё это на каждый набор инструкций."""
    _label, mt, st = SUITES[suite]
    tier_count = sum(1 for t in TIER_ORDER if tiers.get(t)) if tiers else 1
    mt_count = (tier_count if suite == "mttiers" else 1) if mt else 0
    return (mt_count + (tier_count if st else 0)) * profiles


def estimate_seconds(phases: int, duration: float, cooldown: float) -> float:
    """Примерное время прогона: фазы + ~3 с на запуск каждой + паузы между ними."""
    return (duration + 3) * phases + cooldown * max(phases - 1, 0)


def sweep_seconds(steps: int, phases: int, duration: float, cooldown: float,
                  settle: float = DEFAULT_SETTLE) -> float:
    """Время ступенчатого замера: на каждой ступени пауза settle + все фазы."""
    return steps * (settle + estimate_seconds(phases, duration, cooldown))


def _fmt_total(seconds: float) -> str:
    minutes = seconds / 60
    return f"~{seconds:.0f} с" if minutes < 1.5 else f"~{minutes:.0f} мин"


def _tier_names(tiers: dict[str, list[int]] | None) -> str:
    return " / ".join(TIER_NAMES[t] for t in TIER_ORDER if tiers and tiers.get(t))


def _st_desc(tiers: dict[str, list[int]] | None) -> str:
    if not tiers:
        return "однопоток"
    return f"однопоток на {_tier_names(tiers)}-ядрах"


def _ask_isa(profiles: list[str]):
    """Шаг 2 меню: готовые варианты наборов инструкций.

    Возвращает список профилей или "back". Если CPU умеет только обычный
    профиль, шаг не показывается."""
    if profiles == ["scalar"]:
        return ["scalar"]
    shown: dict[str, tuple[str, list[str]]] = {}
    for key, (label, want) in ISA_PRESETS.items():
        if key == "2":  # «все наборы» — все, что умеет этот CPU
            if len(profiles) > 1:
                shown[key] = (label, list(profiles))
        elif all(w in profiles for w in want):
            shown[key] = (label, want)
    print("Шаг 2. Какие наборы инструкций?")
    for key, (label, want) in shown.items():
        what = ", ".join(core.PROFILE_LABELS[w] for w in want)
        print(f"  {key}. {label:<16} — {what}")
    print("  0. Назад")
    choice = util.ask_choice("Выбор [1]: ", set(shown) | {"0"}, "1")
    print()
    if choice == "0":
        return "back"
    return [w for w in shown[choice][1] if w in profiles]


def _sweep_cost(limiter, phases: int, has_st: bool) -> tuple[int, float]:
    """(фаз на ступень, доп. секунд на ступень) для оценки времени замера."""
    if getattr(limiter, "mt_only", False) and has_st:
        phases = 1
    return phases, getattr(limiter, "step_overhead_s", 0.0)


def _ask_sweep(limiter, phases: int, duration: float, cooldown: float,
               settle: float = DEFAULT_SETTLE, manual=None,
               has_mt: bool = True, has_st: bool = False):
    """Шаги 3–4 меню: ступени и (если есть выбор) способ выставлять лимит.

    Возвращает (ступени, ограничитель), None (без замера) или "back".
    manual — запасной ручной ограничитель (Windows: ThrottleStop / XTU)."""
    auto = limiter
    reason = limiter.problem()
    if not reason and getattr(limiter, "mt_only", False) and not has_mt:
        reason = "авто-режим ограничивает только многопоток"
    if reason:
        if manual is None:
            print(f"Ступенчатый замер мощности недоступен: {reason}.")
            print()
            return None
        auto = None
        limiter = manual
    default = list(powerlimit.DEFAULT_STEPS)
    n_ph, extra = _sweep_cost(limiter, phases, has_st)
    total = _fmt_total(sweep_seconds(len(default), n_ph, duration, cooldown, settle + extra))
    print("Шаг 4. Ступенчатый замер мощности (PPW на разных лимитах)?")
    if auto is not None and manual is not None:
        print(f"   Лимиты: {auto.describe()}; или вручную ({manual.TOOLS})")
    else:
        print(f"   Лимиты: {limiter.describe()}")
        if reason:
            print(f"   (автоматически нельзя: {reason})")
    print("  1. Нет")
    print(f"  2. Да: {powerlimit.fmt_steps(default)}  (всего {total})")
    print("  3. Да, свои ступени")
    print("  0. Назад")
    choice = util.ask_choice("Выбор [1]: ", {"0", "1", "2", "3"}, "1")
    print()
    if choice == "0":
        return "back"
    if choice == "1":
        return None
    steps = default
    while choice == "3":
        try:
            raw = input("Ступени в ваттах через запятую (например 10,15,25): ")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        try:
            steps = powerlimit.parse_steps(raw)
        except ValueError as exc:
            print(f"Ошибка: {exc}")
            continue
        total = _fmt_total(sweep_seconds(len(steps), n_ph, duration, cooldown, settle + extra))
        print(f"Ступени: {powerlimit.fmt_steps(steps)}, всего {total}")
        print()
        break
    if auto is not None and manual is not None:
        print("Шаг 5. Как выставлять лимит?")
        print(f"  1. Автоматически — {auto.describe()}")
        print(f"  2. Вручную       — {manual.describe()}")
        if getattr(auto, "mt_only", False) and has_st:
            print("     (в авто-режиме однопоточные фазы в замер не входят)")
        mode = util.ask_choice("Выбор [1]: ", {"1", "2"}, "1")
        print()
        limiter = auto if mode == "1" else manual
    return steps, limiter


def interactive_menu(reader, tiers: dict[str, list[int]] | None,
                     cooldown: float = DEFAULT_COOLDOWN, limiter=None,
                     settle: float = DEFAULT_SETTLE, manual=None,
                     profiles: list[str] | None = None) -> dict | None:
    """Интерактивное меню: какой набор тестов, сколько секунд на фазу и (если
    передан limiter) нужен ли ступенчатый замер мощности и как ставить лимит.

    Возвращает {"suite", "duration", "pin", "mt_pin", "no_st", "only_st",
    "sweep", "limiter", "isa"} либо None (выход). На гибридных CPU однопоточная
    фаза идёт на каждом типе ядер, а «многопоток по ядрам» нагружает по очереди
    все P-, все E- и все LP-E-ядра. profiles — профили, которые умеет CPU."""
    profiles = profiles or ["scalar"]
    st_desc = _st_desc(tiers)
    descs = {
        "cpu": f"многопоток + {st_desc}",
        "mt": "все потоки",
        "mttiers": f"все {_tier_names(tiers)}-ядра по очереди",
        "st": st_desc,
    }
    # нумерация постоянная; «по ядрам» есть только у гибридных CPU
    suite_keys = {"1": "cpu", "2": "mt", "3": "mttiers", "4": "st"}
    if not tiers:
        del suite_keys["3"]
    print(f"PPW Bench {__version__} — интерактивный режим")
    print()
    while True:
        print("Шаг 1. Какой тест запустить?")
        for num, key in suite_keys.items():
            print(f"  {num}. {SUITES[key][0]:<19} — {descs[key]}")
        print("  5. Показать датчики мощности")
        print("  0. Выход")
        choice = util.ask_choice("Выбор [1]: ", set(suite_keys) | {"0", "5"}, "1")
        print()
        if choice == "0":
            return None
        if choice == "5":
            print_sensors(reader)
            print()
            util.pause("Нажмите Enter, чтобы вернуться в меню...")
            print()
            continue
        suite = suite_keys[choice]
        isa = _ask_isa(profiles)
        if isa == "back":
            continue
        phases = suite_phase_count(suite, tiers, len(isa))

        print("Шаг 3. Сколько секунд на каждую фазу?")
        for i, d in enumerate(MENU_DURATIONS, 1):
            total = _fmt_total(estimate_seconds(phases, d, cooldown))
            print(f"  {i}. {d:>3.0f} с   (фаз: {phases}, всего {total})")
        print("  0. Назад")
        dchoice = util.ask_choice("Выбор [2]: ", {"0", "1", "2", "3"}, "2")
        print()
        if dchoice == "0":
            continue
        duration = MENU_DURATIONS[int(dchoice) - 1]
        _label, mt, st = SUITES[suite]
        sweep, chosen_limiter = None, limiter
        if limiter is not None:
            ans = _ask_sweep(limiter, phases, duration, cooldown, settle, manual,
                             has_mt=mt, has_st=st)
            if ans == "back":
                continue
            if ans:
                sweep, chosen_limiter = ans
        return {
            "suite": suite,
            "duration": duration,
            "sweep": sweep,
            "limiter": chosen_limiter,
            "isa": isa,
            "pin": "all" if st and tiers else "auto",
            "mt_pin": "all" if suite == "mttiers" else "none",
            "no_st": not st,
            "only_st": not mt,
        }


PIN_MODES = ("auto", "p", "e", "lpe", "all", "none")


def mt_pin_arg(value: str) -> str:
    v = pin_arg(value)
    if v == "auto":
        raise argparse.ArgumentTypeError("для --mt-pin: none, all, p, e, lpe или список CPU")
    return v


def pin_arg(value: str) -> str:
    v = value.strip().lower()
    if v in PIN_MODES:
        return v
    try:
        if parse_cpulist(v):
            return v
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(
        "ожидается auto, p, e, lpe, all, none или список CPU вида 0,2,4-7 "
        f"(получено {value!r})"
    )


def _tier_phase(tier: str, cpus: list[int], tag: str = "ST", auto: bool = False) -> dict:
    name = TIER_NAMES[tier]
    label = f"{name}-ядра (CPU {format_cpulist(cpus)})" + (", авто" if auto else "")
    return {"tag": tag, "cpus": cpus, "label": label, "core_kind": name}


def resolve_st(mode: str, backend, tag: str = "ST",
               flag: str = "--pin") -> tuple[list[dict], list[str]]:
    """Режим --pin (или --mt-pin при tag="MT") -> (фазы, предупреждения).

    Фаза: {"tag", "cpus", "label", "core_kind"}. На гибридных CPU планировщик
    переносит однопоточную нагрузку между ядрами разных типов, и результат
    прыгает от прогона к прогону: auto закрепляет её на P-ядрах, p/e/lpe — на
    ядрах выбранного типа, all — запускает отдельную фазу на каждом типе."""
    what = f"{tag}-фаза"
    plain = {"tag": tag, "cpus": None, "label": "без закрепления", "core_kind": None}
    if mode == "none":
        return [plain], []
    if mode in ("auto", "p", "e", "lpe", "all"):
        types = backend.core_types()
        if not types:
            if mode == "auto":
                return [dict(plain, label="без закрепления (CPU не гибридный)")], []
            return [plain], [
                f"{flag} {mode}: P/E-ядра не обнаружены (CPU не гибридный), "
                f"{what} идёт без закрепления."
            ]
        if mode == "auto":
            return [_tier_phase("p", types["p"], tag=tag, auto=True)], []
        if mode == "all":
            return [_tier_phase(t, types[t], tag=f"{tag}-{TIER_NAMES[t]}")
                    for t in TIER_ORDER if types.get(t)], []
        if not types.get(mode):
            found = ", ".join(TIER_NAMES[t] for t in TIER_ORDER if types.get(t))
            return [plain], [
                f"{flag} {mode}: {TIER_NAMES[mode]}-ядра не обнаружены "
                f"(есть: {found}), {what} идёт без закрепления."
            ]
        return [_tier_phase(mode, types[mode], tag=tag)], []
    cpus = parse_cpulist(mode)
    return [dict(plain, cpus=cpus, label=f"CPU {format_cpulist(cpus)}")], []


def resolve_mt(mode: str, backend) -> tuple[list[dict], list[str]]:
    """Режим --mt-pin -> (многопоточные фазы, предупреждения). none — одна фаза
    на всех ядрах; all — по фазе на каждый тип ядер (все P, все E, все LP-E)."""
    phases, warns = resolve_st(mode, backend, tag="MT", flag="--mt-pin")
    return [dict(ph, label=None) if ph["cpus"] is None else ph for ph in phases], warns


def resolve_isa(requested: list[str], supported: list[str]) -> tuple[list[str], list[str]]:
    """--isa -> (профили фаз в порядке PROFILES, предупреждения о неподдерживаемых)."""
    if "all" in requested:
        return [p for p in core.PROFILES if p in supported], []
    out, warns = [], []
    for p in core.PROFILES:
        if p not in requested:
            continue
        if p in supported:
            out.append(p)
        else:
            warns.append(f"--isa {p}: {core.PROFILE_LABELS[p]} не поддерживается этим CPU — "
                         "фазы с ним пропущены.")
    return out, warns


def resolve_pin(mode: str, backend) -> tuple[list[int] | None, str, list[str]]:
    """Совместимость: одна ST-фаза -> (CPU, подпись, предупреждения)."""
    phases, warns = resolve_st(mode, backend)
    return phases[0]["cpus"], phases[0]["label"], warns


def warmup_arg(value: str) -> float | None:
    """--warmup auto | N (секунд)."""
    if value.strip().lower() == "auto":
        return None
    try:
        sec = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидается auto или число секунд (получено {value!r})") from None
    if sec < 0:
        raise argparse.ArgumentTypeError("прогрев не может быть отрицательным")
    return sec


def isa_arg(value: str) -> list[str]:
    """--isa scalar,avx2,aes | all -> список профилей (all раскрывается позже)."""
    names = [v.strip().lower() for v in value.split(",") if v.strip()]
    bad = [n for n in names if n not in core.PROFILES + ("all",)]
    if not names or bad:
        raise argparse.ArgumentTypeError(
            f"ожидается all или список из {', '.join(core.PROFILES)} (получено {value!r})")
    return names


def sweep_arg(value: str) -> list[float]:
    try:
        return powerlimit.parse_steps(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


@contextlib.contextmanager
def exit_on_signals():
    """SIGTERM / SIGHUP (закрыли терминал, kill) -> SystemExit, чтобы сработали
    finally-блоки и лимиты мощности вернулись к исходным."""
    old = {}

    def handler(signum, _frame):
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            old[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):
            pass
    try:
        yield
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ppw-bench",
        description="Тест Performance-per-Watt (PPW) для CPU",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--duration", type=float, default=60.0, help="секунд на фазу")
    ap.add_argument("--quick", action="store_true", help="быстрый режим: 20 с на фазу")
    ap.add_argument("--threads", type=int, default=0, help="потоков в MT-фазе (0 = все)")
    phase_group = ap.add_mutually_exclusive_group()
    phase_group.add_argument("--no-st", action="store_true", help="не запускать single-thread фазу")
    phase_group.add_argument("--only-st", action="store_true", help="только single-thread фаза")
    ap.add_argument("--warmup", type=warmup_arg, default=None, metavar="auto|N",
                    help="прогрев в начале фазы, который не входит в замер: auto (по умолчанию) — "
                         "не меньше 25 %% фазы (мин. 8 с), а если мощность ещё падала "
                         "(конец буста PL2) — до момента, когда она установилась; N — ровно N с")
    ap.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN,
                    help="пауза между фазами, с (0 = без паузы)")
    ap.add_argument("--pin", type=pin_arg, default="auto",
                    help="ядра для single-thread фазы: auto (P-ядра на гибридных CPU), "
                         "p, e, lpe, all (отдельная фаза на каждом типе ядер), none "
                         "или список CPU, например 0,2,4-7")
    ap.add_argument("--isa", type=isa_arg, default=["scalar"], metavar="LIST",
                    help="наборы инструкций: scalar (обычный код), avx2 (AVX2+FMA, "
                         "GFLOPS), aes (AES-NI, GB/s) или all — каждая фаза (MT и ST) "
                         "повторяется на каждом поддерживаемом наборе")
    ap.add_argument("--mt-pin", type=mt_pin_arg, default="none",
                    help="ядра для многопоточной фазы: none (все, по умолчанию), all "
                         "(отдельная фаза на всех P-, всех E- и всех LP-E-ядрах), p, e, "
                         "lpe или список CPU")
    ap.add_argument("--repeat", type=int, default=1,
                    help="повторить каждую фазу N раз; в отчёте медианы и разброс")
    ap.add_argument("--watts", type=float, default=None,
                    help="задать мощность CPU вручную (если датчики недоступны)")
    ap.add_argument("--sweep", type=sweep_arg, default=None, metavar="W,W,...",
                    help="ступенчатый замер: прогнать фазы на каждом лимите мощности "
                         "(PL1 = PL2), например 5,8,10,15,20,28,35,45. Linux (Intel): "
                         "лимиты выставляются сами (нужен root); Windows: потолком частоты "
                         "или вручную, см. --sweep-mode")
    ap.add_argument("--sweep-mode", choices=powerlimit.SWEEP_MODES, default="auto",
                    help="как выставлять лимит: auto — сам (Linux powercap; Windows — "
                         "потолок частоты, только многопоток), manual — вручную "
                         "(ThrottleStop / XTU)")
    ap.add_argument("--settle", type=float, default=DEFAULT_SETTLE,
                    help="пауза после смены лимита перед фазами, с")
    ap.add_argument("--out", type=str, default=None, help="путь к .txt-отчёту")
    ap.add_argument("--list", action="store_true", help="показать датчики и выйти")
    ap.add_argument("--interactive", action="store_true", help="запустить интерактивное меню")
    ap.add_argument("--version", action="version", version=f"ppw-bench {__version__}")
    return ap


def run_sweep(limiter, steps: list[float], settle: float, run_plan, warnings: list[str]) -> list[dict]:
    """Прогоняет run_plan() на каждой ступени лимита мощности.

    Исходные лимиты возвращаются в любом случае: при ошибке, Ctrl+C, SIGTERM.
    Если лимит не принят (заблокирован в BIOS и т.п.), замер останавливается,
    а уже снятые ступени остаются в отчёте. Недостижимая ступень (StepSkipped)
    попадает в отчёт как пропущенная, замер идёт дальше."""
    limiter.save()
    for line in limiter.saved_lines():
        print(f"Исходный лимит {line} — будет возвращён после замера")
    done: list[dict] = []
    try:
        with exit_on_signals():
            for i, watts in enumerate(steps):
                print()
                print(f"=== Ступень {i + 1}/{len(steps)}: {watts:g} Вт ===", flush=True)
                limiter.last_note = None
                try:
                    applied = limiter.apply(watts)
                except powerlimit.StepSkipped as exc:
                    print(f"  ! Ступень {watts:g} Вт пропущена: {exc}")
                    done.append({"target": watts, "applied": None, "phases": [],
                                 "changed": [], "skipped": str(exc)})
                    continue
                except powerlimit.LimitError as exc:
                    msg = f"Ступенчатый замер остановлен на {watts:g} Вт: {exc}"
                    print(f"  ! {msg}")
                    warnings.append(msg)
                    break
                if settle > 0:
                    print(f"     пауза {settle:.0f} с на новом лимите ...", flush=True)
                    time.sleep(settle)
                phases = run_plan()
                changed = limiter.verify()
                for c in changed:
                    msg = f"Ступень {watts:g} Вт: лимит изменила другая программа: {c}"
                    print(f"  ! {msg}")
                    warnings.append(msg)
                for ph in phases:
                    ph["limit_held"] = powerlimit.limit_held(watts, ph.get("pkg_w"))
                done.append({"target": watts, "applied": applied, "phases": phases,
                             "changed": changed, "note": limiter.last_note})
    finally:
        errors = limiter.restore()
        if errors:
            for e in errors:
                print(f"  !!! Не удалось вернуть лимит: {e}")
            warnings.append("Исходные лимиты вернуть не удалось — проверьте их вручную "
                            "или перезагрузитесь.")
        elif limiter.mode != "manual":
            print("Исходные лимиты мощности возвращены.")
    return done


def main() -> int:
    util.setup_console()
    ap = build_parser()
    args = ap.parse_args()
    backend = current_backend()

    interactive = args.interactive or util.is_interactive()
    duration = 20.0 if args.quick else args.duration
    if args.cooldown < 0:
        ap.error("--cooldown не может быть отрицательным")
    if not 1 <= args.repeat <= 20:
        ap.error("--repeat должен быть от 1 до 20")
    if args.settle < 0:
        ap.error("--settle не может быть отрицательным")
    if args.interactive and args.sweep:
        ap.error("--sweep нельзя сочетать с --interactive: ступени выбираются в меню (шаг 3)")

    if backend.NAME == "Windows":
        # прошлый ступенчатый замер мог оборваться с выставленным потолком частоты
        for msg in powerlimit.recover_pending():
            print(f"  ! {msg}")

    reader = backend.PowerReader()
    reader.prime()
    pin_mode = args.pin
    limiter = powerlimit.for_backend(backend, reader, args.sweep_mode)
    # Windows: если авто-режим не подходит, лимит можно выставить вручную
    manual_alt = (powerlimit.ManualLimiter()
                  if backend.NAME == "Windows" and limiter.mode != "manual" else None)
    sweep = args.sweep
    exe = core.ensure_bench(backend)
    supported = core.bench_profiles(exe)
    isa = args.isa
    if interactive:
        chosen = interactive_menu(reader, backend.core_types(), args.cooldown, limiter,
                                  args.settle, manual_alt, supported)
        if chosen is None:
            return 0
        duration = chosen["duration"]
        pin_mode = chosen["pin"]
        args.no_st = chosen["no_st"]
        args.only_st = chosen["only_st"]
        args.mt_pin = chosen["mt_pin"]
        sweep = chosen["sweep"]
        limiter = chosen["limiter"] or limiter
        isa = chosen["isa"]
    else:
        if duration < 8:
            ap.error("--duration должен быть >= 8 секунд")
        if args.list:
            print_sensors(reader)
            return 0
        if sweep:
            problem = limiter.problem()
            if problem:
                ap.error(f"--sweep: {problem}")
            if limiter.mode == "manual" and not util.stdin_is_tty():
                ap.error("--sweep: на этой платформе лимит выставляется вручную, "
                         "нужен интерактивный терминал")
            if getattr(limiter, "mt_only", False) and args.only_st:
                ap.error("--sweep: авто-режим ограничивает только многопоток; "
                         "для однопотока добавьте --sweep-mode manual")

    logical = backend.logical_cpus()
    mt_threads = args.threads if args.threads > 0 else logical

    # источник мощности
    manual = args.watts
    kind = reader.kind()
    if manual:
        source = f"вручную (--watts {manual:g} Вт)"
    elif kind == "pkg":
        source = f"{reader.describe()}: мощность пакета CPU"
    elif kind == "sys":
        source = f"{reader.describe()}: мощность системы (RAPL пакета недоступен)"
    else:
        source = f"{reader.describe()} — PPW не будет посчитан"

    power_note = {
        0: "от батареи (для максимальной производительности подключите адаптер)",
        1: "от сети",
    }.get(backend.ac_line_status(), "неизвестно")

    warnings = backend.environment_warnings()
    if not manual:
        warnings += reader.hints()

    st_phases, pin_warnings = resolve_st(pin_mode, backend)
    warnings += pin_warnings
    profiles, isa_warnings = resolve_isa(isa, supported)
    warnings += isa_warnings
    if not profiles:
        ap.error("--isa: ни один из выбранных наборов не поддерживается этим CPU")
    mt_phases, mt_warnings = resolve_mt(args.mt_pin, backend)
    warnings += mt_warnings
    # базовые фазы (где и сколько потоков) x наборы инструкций, «по фазам»:
    # MT обычный -> MT AVX2 -> ... -> ST-P обычный -> ST-P AVX2 -> ...
    bases: list[dict] = []
    if not args.only_st:
        bases += [dict(ph, threads=len(ph["cpus"]) if ph["cpus"] else mt_threads)
                  for ph in mt_phases]
    if not args.no_st:
        bases += [dict(ph, threads=1) for ph in st_phases]
    phases_spec: list[dict] = []
    for base in bases:
        for prof in profiles:
            tag = base["tag"] if prof == "scalar" else f"{base['tag']}-{prof.upper()}"
            phases_spec.append(dict(base, tag=tag, profile=prof))
    if sweep and getattr(limiter, "mt_only", False):
        dropped = [ph for ph in phases_spec if ph["threads"] == 1]
        phases_spec = [ph for ph in phases_spec if ph["threads"] > 1]
        if dropped:
            warnings.append(
                "Авто-режим ограничивает только многопоток: однопоточные фазы в "
                "ступенчатый замер не вошли (для них — --sweep-mode manual)."
            )

    repeat = args.repeat
    plan = [(r, ph) for r in range(repeat) for ph in phases_spec]
    cooldown = args.cooldown if len(plan) > 1 else 0.0
    meta = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cpu": backend.cpu_name(),
        "cores": backend.physical_cores() or logical,
        "threads": logical,
        "os": platform.platform(),
        "power": power_note,
        "profile": backend.power_profile(),
        "duration": duration,
        "warmup": core.warmup_seconds(duration) if args.warmup is None else args.warmup,
        "warmup_auto": args.warmup is None,
        "cooldown": cooldown,
        "repeat": repeat,
        "version": __version__,
        "warnings": warnings,
    }
    if sweep:
        meta["sweep"] = limiter.describe()
        meta["settle"] = args.settle

    total_time = estimate_seconds(len(plan), duration, cooldown)
    step_extra = args.settle + getattr(limiter, "step_overhead_s", 0.0)
    print(f"CPU: {meta['cpu']}  ({meta['cores']} ядер / {meta['threads']} потоков)")
    print(f"Мощность: {source}")
    print(f"Питание: {power_note}; {meta['profile']}")
    for w in warnings:
        print(f"  ! {w}")
    print(
        f"План: {len(phases_spec)} фаза(ы)"
        + (f" × {repeat} повтора(ов)" if repeat > 1 else "")
        + f" по {duration:.0f} с (прогрев {'от ' if meta['warmup_auto'] else ''}"
          f"{meta['warmup']:.0f} с), ~{total_time:.0f} с всего"
        + (f" на ступень; ступени {powerlimit.fmt_steps(sweep)}, всего "
           f"{_fmt_total(sweep_seconds(len(sweep), len(plan), duration, cooldown, step_extra))}"
           if sweep else "")
    )
    for ph in phases_spec:
        if ph["label"] and ph["profile"] == profiles[0]:
            print(f"{ph['tag']}: {ph['label']}")
    if sweep:
        print(f"Лимиты: {limiter.describe()}")
    print()

    tiers = backend.core_types()
    sampler = core.Sampler(reader)
    sampler.start()
    if sweep and hasattr(limiter, "attach"):
        limiter.attach(exe, sampler, mt_threads)

    def run_plan() -> list[dict]:
        runs: dict[str, list[dict]] = {ph["tag"]: [] for ph in phases_spec}
        for idx, (r, ph) in enumerate(plan):
            if idx > 0:
                core.cooldown(cooldown)
            threads, tag, cpus = ph["threads"], ph["tag"], ph["cpus"]
            label = "MULTI-THREAD" if threads > 1 else "SINGLE-THREAD"
            if ph["profile"] != "scalar":
                label += f", {core.PROFILE_LABELS[ph['profile']]}"
            if ph["core_kind"]:
                label += f", {ph['core_kind']}-ядр{'о' if threads == 1 else 'а'}"
            rep = f" #{r + 1}/{repeat}" if repeat > 1 else ""
            print(f"[{tag}{rep}] {label}, {threads} поток(ов), {duration:.0f} с ...", flush=True)
            res = core.apply_power_basis(
                core.run_phase(exe, threads, duration, sampler, backend=backend, cpus=cpus,
                               tiers=tiers, profile=ph["profile"], warmup=args.warmup),
                manual,
            )
            res["tag"] = tag
            res["core_kind"] = ph["core_kind"]
            if cpus or threads == 1:
                if cpus and not res["cpus"]:
                    res["pin_label"] = "без закрепления (не удалось задать аффинити)"
                    res["core_kind"] = None
                else:
                    res["pin_label"] = ph["label"]
            runs[tag].append(res)
            ppw = res["ppw"]
            power = res["power_w"]
            freq = res.get("freq")
            print(
                f"     {report.fmt_rate(res, res['work_per_s'])}"
                + (f"   {power:.1f} Вт" if power else "")
                + (f"   PPW {report.fmt_ppw(res, ppw)}" if ppw else "")
                + (f"   {freq['mhz'] / 1000:.2f} ГГц" if freq else ""),
                flush=True,
            )
            if res.get("settled") and res["warmup"] > meta["warmup"] + 0.5:
                print(f"     прогрев {res['warmup']:.0f} с: мощность установилась не сразу", flush=True)
            note = report.instability_note(res)
            if note:
                print(f"     ! {note}", flush=True)
        return [core.aggregate_runs(runs[ph["tag"]]) for ph in phases_spec]

    try:
        if sweep:
            steps = run_sweep(limiter, sweep, args.settle, run_plan, warnings)
        else:
            results = run_plan()
    finally:
        sampler.stop()

    if sweep:
        text = report.build_sweep_report(meta, steps, source)
    else:
        text = report.build_report(meta, results, source)
    print()
    print(text)
    print()

    save = util.ask_yes_no("Сохранить отчёт в файл?", default=True) if interactive else True
    if save:
        out = report.save_report(text, args.out)
        print(f"Отчёт сохранён: {out}")
        if sweep:
            csv_path = report.save_sweep_csv(steps, out.with_suffix(".csv"))
            if csv_path:
                print(f"Таблица ступеней (CSV): {csv_path}")
    else:
        print("Отчёт не сохранён (остался только в консоли).")

    # На Windows окно консоли закрывается сразу после выхода — даём прочитать.
    if interactive and util.IS_WINDOWS:
        util.pause()
    return 0


def entry() -> None:
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        raise SystemExit(130)
