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
from ppwbench.cpusets import format_cpulist, parse_cpulist
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


def interactive_menu(reader) -> float | None:
    """Спрашивает режим теста. Возвращает длительность фазы или None (выход)."""
    durations = {"2": 20.0, "3": 60.0, "4": 120.0}
    print(f"PPW Bench {__version__} — интерактивный режим")
    print()
    while True:
        print("Что запустить?")
        print("  1. Показать датчики мощности")
        print("  2. Быстрый тест      — 20 с на фазу")
        print("  3. Обычный тест      — 60 с на фазу")
        print("  4. Длительный тест   — 120 с на фазу")
        print("  0. Выход")
        choice = util.ask_choice("Выбор [3]: ", {"0", "1", "2", "3", "4"}, "3")
        print()
        if choice == "0":
            return None
        if choice == "1":
            print_sensors(reader)
            print()
            util.pause("Нажмите Enter, чтобы вернуться в меню...")
            print()
            continue
        return durations[choice]


PIN_MODES = ("auto", "p", "e", "none")


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
        f"ожидается auto, p, e, none или список CPU вида 0,2,4-7 (получено {value!r})"
    )


def resolve_pin(mode: str, backend) -> tuple[list[int] | None, str, list[str]]:
    """Режим --pin -> (CPU для ST-фазы, подпись для отчёта, предупреждения).

    На гибридных CPU (P + E-ядра) планировщик переносит однопоточную
    нагрузку между ядрами разных типов, и ST-результат прыгает от прогона к
    прогону. auto закрепляет ST-фазу на P-ядрах, если CPU гибридный."""
    if mode == "none":
        return None, "без закрепления", []
    if mode in ("auto", "p", "e"):
        types = backend.core_types()
        if not types:
            if mode == "auto":
                return None, "без закрепления (CPU не гибридный)", []
            return None, "без закрепления", [
                f"--pin {mode}: P/E-ядра не обнаружены (CPU не гибридный), "
                "ST-фаза идёт без закрепления."
            ]
        kind = "p" if mode == "auto" else mode
        cpus = types[kind]
        label = f"{'P' if kind == 'p' else 'E'}-ядра (CPU {format_cpulist(cpus)})"
        if mode == "auto":
            label += ", авто"
        return cpus, label, []
    cpus = parse_cpulist(mode)
    return cpus, f"CPU {format_cpulist(cpus)}", []


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
                         "p, e, none или список CPU, например 0,2,4-7")
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
    if interactive:
        chosen = interactive_menu(reader)
        if chosen is None:
            return 0
        duration = chosen
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

    phases_spec: list[tuple[int, str]] = []
    if not args.only_st:
        phases_spec.append((mt_threads, "MT"))
    if not args.no_st:
        phases_spec.append((1, "ST"))

    st_cpus, pin_label, pin_warnings = resolve_pin(args.pin, backend)
    warnings += pin_warnings
    repeat = args.repeat
    plan = [(r, threads, tag) for r in range(repeat) for threads, tag in phases_spec]
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
    if not args.no_st:
        print(f"ST-фаза: {pin_label}")
    print()

    sampler = core.Sampler(reader)
    sampler.start()
    runs: dict[str, list[dict]] = {tag: [] for _, tag in phases_spec}
    try:
        for idx, (r, threads, tag) in enumerate(plan):
            if idx > 0:
                core.cooldown(cooldown)
            label = "MULTI-THREAD" if threads > 1 else "SINGLE-THREAD"
            rep = f" #{r + 1}/{repeat}" if repeat > 1 else ""
            print(f"[{tag}{rep}] {label}, {threads} поток(ов), {duration:.0f} с ...", flush=True)
            cpus = st_cpus if tag == "ST" else None
            res = core.apply_power_basis(
                core.run_phase(exe, threads, duration, sampler, backend=backend, cpus=cpus),
                manual,
            )
            if tag == "ST":
                if cpus and not res["cpus"]:
                    res["pin_label"] = "без закрепления (не удалось задать аффинити)"
                else:
                    res["pin_label"] = pin_label
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
    results = [core.aggregate_runs(runs[tag]) for _, tag in phases_spec]

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
