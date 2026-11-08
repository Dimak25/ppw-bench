# -*- coding: utf-8 -*-
"""Командная строка и интерактивное меню.

    ppw-bench                       # меню (из консоли без аргументов)
    ppw-bench --quick               # 20 с на фазу
    ppw-bench --duration 120        # длиннее = стабильнее (PL1)
    ppw-bench --threads 8 --no-st   # только MT на 8 потоках
    ppw-bench --watts 45            # если датчиков нет: задать мощность вручную
    ppw-bench --list                # показать найденные датчики и выйти
"""

from __future__ import annotations

import argparse
import platform
from datetime import datetime

from ppwbench import __version__, core, report, util
from ppwbench.cpusets import TIER_NAMES, TIER_ORDER, format_cpulist, parse_cpulist
from ppwbench.platforms import current as current_backend
from ppwbench.util import fmt_si

DEFAULT_COOLDOWN = 10.0


def print_sensors(reader) -> None:
    print("Доступные источники мощности:")
    print("  ", reader.describe())
    for line in reader.sensor_lines():
        print("  ", line)
    for hint in reader.hints():
        print(f"   ! {hint}")


def interactive_menu(reader, hybrid: bool) -> tuple[float, str | None] | None:
    """Спрашивает режим теста.

    Возвращает (длительность фазы, режим --pin или None) либо None (выход).
    На гибридных CPU есть пункт «ST на каждом типе ядер»."""
    durations = {"2": 20.0, "3": 60.0, "4": 120.0}
    choices = {"0", "1", "2", "3", "4"} | ({"5"} if hybrid else set())
    print(f"PPW Bench {__version__} — интерактивный режим")
    print()
    while True:
        print("Что запустить?")
        print("  1. Показать датчики мощности")
        print("  2. Быстрый тест      — 20 с на фазу")
        print("  3. Обычный тест      — 60 с на фазу")
        print("  4. Длительный тест   — 120 с на фазу")
        if hybrid:
            print("  5. Один поток на каждом типе ядер (P / E / LP-E) — по 30 с")
        print("  0. Выход")
        choice = util.ask_choice("Выбор [3]: ", choices, "3")
        print()
        if choice == "0":
            return None
        if choice == "1":
            print_sensors(reader)
            print()
            util.pause("Нажмите Enter, чтобы вернуться в меню...")
            print()
            continue
        if choice == "5":
            return 30.0, "all"
        return durations[choice], None


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
    ap.add_argument("--out", type=str, default=None, help="путь к .txt-отчёту")
    ap.add_argument("--list", action="store_true", help="показать датчики и выйти")
    ap.add_argument("--interactive", action="store_true", help="запустить интерактивное меню")
    ap.add_argument("--version", action="version", version=f"ppw-bench {__version__}")
    return ap


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

    reader = backend.PowerReader()
    reader.prime()
    pin_mode = args.pin
    if interactive:
        chosen = interactive_menu(reader, hybrid=backend.core_types() is not None)
        if chosen is None:
            return 0
        duration, menu_pin = chosen
        if menu_pin:
            pin_mode = menu_pin
            args.only_st = True
    else:
        if duration < 8:
            ap.error("--duration должен быть >= 8 секунд")
        if args.list:
            print_sensors(reader)
            return 0

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

    total_time = (duration + 3) * len(plan) + cooldown * (len(plan) - 1)
    print(f"CPU: {meta['cpu']}  ({meta['cores']} ядер / {meta['threads']} потоков)")
    print(f"Мощность: {source}")
    print(f"Питание: {power_note}; {meta['profile']}")
    for w in warnings:
        print(f"  ! {w}")
    print(
        f"План: {len(phases_spec)} фаза(ы)"
        + (f" × {repeat} повтора(ов)" if repeat > 1 else "")
        + f" по {duration:.0f} с (прогрев {meta['warmup']:.0f} с), ~{total_time:.0f} с всего"
    )
    for ph in phases_spec:
        if ph["threads"] == 1:
            print(f"{ph['tag']}: {ph['label']}")
    print()

    sampler = core.Sampler(reader)
    sampler.start()
    runs: dict[str, list[dict]] = {ph["tag"]: [] for ph in phases_spec}
    try:
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
                core.run_phase(exe, threads, duration, sampler, backend=backend, cpus=cpus),
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
            print(
                f"     {fmt_si(res['work_per_s'], 'ед/с')}"
                + (f"   {power:.1f} Вт" if power else "")
                + (f"   PPW {fmt_si(ppw, 'ед/с на Вт')}" if ppw else ""),
                flush=True,
            )
    finally:
        sampler.stop()
    results = [core.aggregate_runs(runs[ph["tag"]]) for ph in phases_spec]

    text = report.build_report(meta, results, source)
    print()
    print(text)
    print()

    save = util.ask_yes_no("Сохранить отчёт в файл?", default=True) if interactive else True
    if save:
        out = report.save_report(text, args.out)
        print(f"Отчёт сохранён: {out}")
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
