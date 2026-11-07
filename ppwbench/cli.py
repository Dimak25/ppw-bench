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

    cooldown = args.cooldown if len(phases_spec) > 1 else 0.0
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
        "version": __version__,
        "warnings": warnings,
    }

    total_time = (duration + 3) * len(phases_spec) + cooldown * (len(phases_spec) - 1)
    print(f"CPU: {meta['cpu']}  ({meta['cores']} ядер / {meta['threads']} потоков)")
    print(f"Мощность: {source}")
    print(f"Питание: {power_note}; {meta['profile']}")
    for w in warnings:
        print(f"  ! {w}")
    print(
        f"План: {len(phases_spec)} фаза(ы) по {duration:.0f} с "
        f"(прогрев {meta['warmup']:.0f} с), ~{total_time:.0f} с всего"
    )
    print()

    sampler = core.Sampler(reader)
    sampler.start()
    results: list[dict] = []
    try:
        for idx, (threads, tag) in enumerate(phases_spec):
            if idx > 0:
                core.cooldown(cooldown)
            label = "MULTI-THREAD" if threads > 1 else "SINGLE-THREAD"
            print(f"[{tag}] {label}, {threads} поток(ов), {duration:.0f} с ...", flush=True)
            res = core.apply_power_basis(core.run_phase(exe, threads, duration, sampler), manual)
            results.append(res)
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
