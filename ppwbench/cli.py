# -*- coding: utf-8 -*-
"""Командная строка и интерактивное меню.

    ppw-bench                       # меню (из консоли без аргументов)
    ppw-bench --quick               # 20 с на фазу
    ppw-bench --duration 120        # длиннее = стабильнее (PL1)
    ppw-bench --threads 8 --no-st   # только MT на 8 потоках
    ppw-bench --watts 45            # если датчиков нет: задать мощность вручную
    ppw-bench --list                # показать найденные датчики и выйти
    sudo ppw-bench --sweep 5,10,15,25 --duration 30   # PPW на разных лимитах (Linux)
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
from ppwbench.util import fmt_si

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
    # ключ: (подпись, MT-фаза, ST-фазы)
    "cpu": ("Весь процессор", True, True),
    "mt": ("Только многопоток", True, False),
    "st": ("Только однопоток", False, True),
}
MENU_DURATIONS = (20.0, 60.0, 120.0)


def suite_phase_count(suite: str, tiers: dict[str, list[int]] | None) -> int:
    """Сколько фаз будет в наборе: MT + по одной ST на каждый тип ядер."""
    _label, mt, st = SUITES[suite]
    st_count = sum(1 for t in TIER_ORDER if tiers.get(t)) if tiers else 1
    return int(mt) + (st_count if st else 0)


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


def _st_desc(tiers: dict[str, list[int]] | None) -> str:
    if not tiers:
        return "однопоток"
    names = " / ".join(TIER_NAMES[t] for t in TIER_ORDER if tiers.get(t))
    return f"однопоток на {names}-ядрах"


def _ask_sweep(limiter, phases: int, duration: float, cooldown: float,
               settle: float = DEFAULT_SETTLE):
    """Шаг 3 меню. Возвращает список ступеней, None (без замера) или "back"."""
    problem = limiter.problem()
    if problem:
        print(f"Ступенчатый замер мощности недоступен: {problem}.")
        print()
        return None
    default = list(powerlimit.DEFAULT_STEPS)
    total = _fmt_total(sweep_seconds(len(default), phases, duration, cooldown, settle))
    print("Шаг 3. Ступенчатый замер мощности (PPW на разных лимитах)?")
    print(f"   Лимиты: {limiter.describe()}")
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
    if choice == "2":
        return default
    while True:
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
        total = _fmt_total(sweep_seconds(len(steps), phases, duration, cooldown, settle))
        print(f"Ступени: {powerlimit.fmt_steps(steps)}, всего {total}")
        print()
        return steps


def interactive_menu(reader, tiers: dict[str, list[int]] | None,
                     cooldown: float = DEFAULT_COOLDOWN, limiter=None,
                     settle: float = DEFAULT_SETTLE) -> dict | None:
    """Интерактивное меню: какой набор тестов, сколько секунд на фазу и (если
    передан limiter) нужен ли ступенчатый замер мощности.

    Возвращает {"suite", "duration", "pin", "no_st", "only_st", "sweep"} либо
    None (выход). На гибридных CPU однопоточная фаза идёт на каждом типе ядер."""
    st_desc = _st_desc(tiers)
    descs = {
        "cpu": f"многопоток + {st_desc}",
        "mt": "все потоки",
        "st": st_desc,
    }
    suite_keys = {"1": "cpu", "2": "mt", "3": "st"}
    print(f"PPW Bench {__version__} — интерактивный режим")
    print()
    while True:
        print("Шаг 1. Какой тест запустить?")
        for num, key in suite_keys.items():
            print(f"  {num}. {SUITES[key][0]:<18} — {descs[key]}")
        print("  4. Показать датчики мощности")
        print("  0. Выход")
        choice = util.ask_choice("Выбор [1]: ", {"0", "1", "2", "3", "4"}, "1")
        print()
        if choice == "0":
            return None
        if choice == "4":
            print_sensors(reader)
            print()
            util.pause("Нажмите Enter, чтобы вернуться в меню...")
            print()
            continue
        suite = suite_keys[choice]
        phases = suite_phase_count(suite, tiers)

        print("Шаг 2. Сколько секунд на каждую фазу?")
        for i, d in enumerate(MENU_DURATIONS, 1):
            total = _fmt_total(estimate_seconds(phases, d, cooldown))
            print(f"  {i}. {d:>3.0f} с   (фаз: {phases}, всего {total})")
        print("  0. Назад")
        dchoice = util.ask_choice("Выбор [2]: ", {"0", "1", "2", "3"}, "2")
        print()
        if dchoice == "0":
            continue
        duration = MENU_DURATIONS[int(dchoice) - 1]
        sweep = None
        if limiter is not None:
            sweep = _ask_sweep(limiter, phases, duration, cooldown, settle)
            if sweep == "back":
                continue
        _label, mt, st = SUITES[suite]
        return {
            "suite": suite,
            "duration": duration,
            "sweep": sweep,
            "pin": "all" if st and tiers else "auto",
            "no_st": not st,
            "only_st": not mt,
        }


PIN_MODES = ("auto", "p", "e", "lpe", "all", "none")


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


def resolve_st(mode: str, backend) -> tuple[list[dict], list[str]]:
    """Режим --pin -> (однопоточные фазы, предупреждения).

    Фаза: {"tag", "cpus", "label", "core_kind"}. На гибридных CPU планировщик
    переносит однопоточную нагрузку между ядрами разных типов, и результат
    прыгает от прогона к прогону: auto закрепляет её на P-ядрах, p/e/lpe — на
    ядрах выбранного типа, all — запускает отдельную фазу на каждом типе."""
    plain = {"tag": "ST", "cpus": None, "label": "без закрепления", "core_kind": None}
    if mode == "none":
        return [plain], []
    if mode in ("auto", "p", "e", "lpe", "all"):
        types = backend.core_types()
        if not types:
            if mode == "auto":
                return [dict(plain, label="без закрепления (CPU не гибридный)")], []
            return [plain], [
                f"--pin {mode}: P/E-ядра не обнаружены (CPU не гибридный), "
                "ST-фаза идёт без закрепления."
            ]
        if mode == "auto":
            return [_tier_phase("p", types["p"], auto=True)], []
        if mode == "all":
            return [_tier_phase(t, types[t], tag=f"ST-{TIER_NAMES[t]}")
                    for t in TIER_ORDER if types.get(t)], []
        if not types.get(mode):
            found = ", ".join(TIER_NAMES[t] for t in TIER_ORDER if types.get(t))
            return [plain], [
                f"--pin {mode}: {TIER_NAMES[mode]}-ядра не обнаружены "
                f"(есть: {found}), ST-фаза идёт без закрепления."
            ]
        return [_tier_phase(mode, types[mode])], []
    cpus = parse_cpulist(mode)
    return [dict(plain, cpus=cpus, label=f"CPU {format_cpulist(cpus)}")], []


def resolve_pin(mode: str, backend) -> tuple[list[int] | None, str, list[str]]:
    """Совместимость: одна ST-фаза -> (CPU, подпись, предупреждения)."""
    phases, warns = resolve_st(mode, backend)
    return phases[0]["cpus"], phases[0]["label"], warns


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
    ap.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN,
                    help="пауза между фазами, с (0 = без паузы)")
    ap.add_argument("--pin", type=pin_arg, default="auto",
                    help="ядра для single-thread фазы: auto (P-ядра на гибридных CPU), "
                         "p, e, lpe, all (отдельная фаза на каждом типе ядер), none "
                         "или список CPU, например 0,2,4-7")
    ap.add_argument("--repeat", type=int, default=1,
                    help="повторить каждую фазу N раз; в отчёте медианы и разброс")
    ap.add_argument("--watts", type=float, default=None,
                    help="задать мощность CPU вручную (если датчики недоступны)")
    ap.add_argument("--sweep", type=sweep_arg, default=None, metavar="W,W,...",
                    help="ступенчатый замер: прогнать фазы на каждом лимите мощности "
                         "(PL1 = PL2), например 5,8,10,15,20,28,35,45. Linux (Intel): "
                         "лимиты выставляются сами (нужен root); Windows: вручную")
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
    а уже снятые ступени остаются в отчёте."""
    limiter.save()
    for line in limiter.saved_lines():
        print(f"Исходный лимит {line} — будет возвращён после замера")
    done: list[dict] = []
    try:
        with exit_on_signals():
            for i, watts in enumerate(steps):
                print()
                print(f"=== Ступень {i + 1}/{len(steps)}: {watts:g} Вт ===", flush=True)
                try:
                    applied = limiter.apply(watts)
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
                    msg = (f"Ступень {watts:g} Вт: лимит изменила другая программа "
                           f"(thermald, TLP, power-profiles-daemon?): {c}")
                    print(f"  ! {msg}")
                    warnings.append(msg)
                for ph in phases:
                    ph["limit_held"] = powerlimit.limit_held(watts, ph.get("pkg_w"))
                done.append({"target": watts, "applied": applied, "phases": phases,
                             "changed": changed})
    finally:
        errors = limiter.restore()
        if errors:
            for e in errors:
                print(f"  !!! Не удалось вернуть лимит: {e}")
            warnings.append("Исходные лимиты вернуть не удалось — проверьте их вручную "
                            "или перезагрузитесь.")
        elif limiter.mode == "auto":
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

    reader = backend.PowerReader()
    reader.prime()
    pin_mode = args.pin
    limiter = powerlimit.for_backend(backend)
    sweep = args.sweep
    if interactive:
        chosen = interactive_menu(reader, backend.core_types(), args.cooldown, limiter,
                                  args.settle)
        if chosen is None:
            return 0
        duration = chosen["duration"]
        pin_mode = chosen["pin"]
        args.no_st = chosen["no_st"]
        args.only_st = chosen["only_st"]
        sweep = chosen["sweep"]
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

    logical = backend.logical_cpus()
    mt_threads = args.threads if args.threads > 0 else logical

    exe = core.ensure_bench(backend)

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
    phases_spec: list[dict] = []
    if not args.only_st:
        phases_spec.append({"tag": "MT", "threads": mt_threads, "cpus": None,
                            "label": None, "core_kind": None})
    if not args.no_st:
        phases_spec += [dict(ph, threads=1) for ph in st_phases]

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
        "warmup": core.warmup_seconds(duration),
        "cooldown": cooldown,
        "repeat": repeat,
        "version": __version__,
        "warnings": warnings,
    }
    if sweep:
        meta["sweep"] = limiter.describe()
        meta["settle"] = args.settle

    total_time = estimate_seconds(len(plan), duration, cooldown)
    print(f"CPU: {meta['cpu']}  ({meta['cores']} ядер / {meta['threads']} потоков)")
    print(f"Мощность: {source}")
    print(f"Питание: {power_note}; {meta['profile']}")
    for w in warnings:
        print(f"  ! {w}")
    print(
        f"План: {len(phases_spec)} фаза(ы)"
        + (f" × {repeat} повтора(ов)" if repeat > 1 else "")
        + f" по {duration:.0f} с (прогрев {meta['warmup']:.0f} с), ~{total_time:.0f} с всего"
        + (f" на ступень; ступени {powerlimit.fmt_steps(sweep)}, всего "
           f"{_fmt_total(sweep_seconds(len(sweep), len(plan), duration, cooldown, args.settle))}"
           if sweep else "")
    )
    for ph in phases_spec:
        if ph["threads"] == 1:
            print(f"{ph['tag']}: {ph['label']}")
    if sweep:
        print(f"Лимиты: {limiter.describe()}")
    print()

    tiers = backend.core_types()
    sampler = core.Sampler(reader)
    sampler.start()

    def run_plan() -> list[dict]:
        runs: dict[str, list[dict]] = {ph["tag"]: [] for ph in phases_spec}
        for idx, (r, ph) in enumerate(plan):
            if idx > 0:
                core.cooldown(cooldown)
            threads, tag, cpus = ph["threads"], ph["tag"], ph["cpus"]
            label = "MULTI-THREAD" if threads > 1 else "SINGLE-THREAD"
            if ph["core_kind"]:
                label += f", {ph['core_kind']}-ядро"
            rep = f" #{r + 1}/{repeat}" if repeat > 1 else ""
            print(f"[{tag}{rep}] {label}, {threads} поток(ов), {duration:.0f} с ...", flush=True)
            res = core.apply_power_basis(
                core.run_phase(exe, threads, duration, sampler, backend=backend, cpus=cpus,
                               tiers=tiers),
                manual,
            )
            res["tag"] = tag
            res["core_kind"] = ph["core_kind"]
            if threads == 1:
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
                f"     {fmt_si(res['work_per_s'], 'ед/с')}"
                + (f"   {power:.1f} Вт" if power else "")
                + (f"   PPW {fmt_si(ppw, 'ед/с на Вт')}" if ppw else "")
                + (f"   {freq['mhz'] / 1000:.2f} ГГц" if freq else ""),
                flush=True,
            )
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
