# -*- coding: utf-8 -*-
"""Текстовый отчёт и его сохранение."""

from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path

from ppwbench import util
from ppwbench.core import BASIS_LABELS
from ppwbench.cpusets import TIER_NAMES, TIER_ORDER
from ppwbench.util import fmt_si

ENERGY_TAGS = {"pkg": "пакет", "sys": "система", "manual": "вручную"}


BAR = "=" * 66
DASH = "-" * 66


def build_report(meta: dict, phases: list[dict], source: str) -> str:
    L = _header(meta, phases, source)
    bar, dash = BAR, DASH

    for i, ph in enumerate(phases, 1):
        kind = "MULTI-THREAD" if ph["threads"] > 1 else "SINGLE-THREAD"
        if ph.get("core_kind"):
            kind += f", {ph['core_kind']}-ядро"
        basis = ph.get("basis")
        L.append(f"ФАЗА {i} — {kind} ({ph['threads']} поток(ов))")
        L.extend(_phase_lines(ph, basis))
        L.append(dash)

    L.append("ИТОГ")
    for ph in phases:
        kind = phase_name(ph)
        if ph.get("ppw"):
            L.append(f"  {kind}: {fmt_si(ph['ppw'], 'ед/с на Вт')} ({BASIS_LABELS[ph['basis']]})")
        else:
            L.append(f"  {kind}: PPW не измерен (нет датчика мощности)")
    L.append(bar)
    L.extend(_legend())
    return "\n".join(L)


def phase_name(ph: dict) -> str:
    """"MT", "ST", "ST (P)" ..."""
    kind = "MT" if ph["threads"] > 1 else "ST"
    if ph.get("core_kind"):
        kind += f" ({ph['core_kind']})"
    return kind


def _legend() -> list[str]:
    return [
        "PPW = сколько единиц работы в секунду даёт каждый ватт.",
        "Чем выше — тем эффективнее. Сравнивайте прогоны при одинаковой",
        "длительности, питании от сети и фоновой нагрузке.",
    ]


def build_sweep_report(meta: dict, steps: list[dict], source: str) -> str:
    """Отчёт ступенчатого замера: таблица «лимит × фаза» и лучший PPW по фазам."""
    all_phases = [ph for st in steps for ph in st["phases"]]
    L = _header(meta, all_phases, source)
    L.append("ПО СТУПЕНЯМ МОЩНОСТИ (лимит PL1 = PL2)")
    if not steps:
        L.append("  Ни одна ступень не была снята.")
    else:
        L.append(f"  {'Лимит':<8}{'Фаза':<11}{'Скорость':<15}{'Мощность':<10}"
                 f"{'PPW':<19}{'Частота'}")
        flagged = False
        for st in steps:
            if st.get("skipped"):
                limit = f"{st['target']:g} Вт"
                L.append(f"  {limit:<8}пропущена: {st['skipped']}")
                continue
            first = True
            for ph in st["phases"]:
                limit = f"{st['target']:g} Вт" if first else ""
                first = False
                power = f"{ph['power_w']:.1f} Вт" if ph.get("power_w") else "n/a"
                mark = ""
                if ph.get("limit_held") is False:
                    mark = " !"
                    flagged = True
                freq = ph.get("freq")
                ghz = f"{freq['mhz'] / 1000:.2f} ГГц" if freq and freq.get("mhz") else ""
                ppw = fmt_si(ph["ppw"], "ед/с/Вт") if ph.get("ppw") else "n/a"
                L.append(f"  {limit:<8}{phase_name(ph):<11}{fmt_si(ph['work_per_s'], 'ед/с'):<15}"
                         f"{power + mark:<10}{ppw:<19}{ghz}".rstrip())
        if flagged:
            L.append("  ! — средняя мощность пакета выше лимита больше чем на 10 %:")
            L.append("      лимит фактически не соблюдался (BIOS, EC или другая программа).")
        notes = [f"{st['target']:g} Вт → {st['note']}" for st in steps if st.get("note")]
        if notes:
            L.append("  Как выставлен лимит:")
            L.extend(f"    {n}" for n in notes)
    L.append(DASH)

    L.append("ЛУЧШИЙ PPW")
    names: list[str] = []
    for ph in all_phases:
        if phase_name(ph) not in names:
            names.append(phase_name(ph))
    for name in names:
        cands = [(st["target"], ph) for st in steps for ph in st["phases"]
                 if phase_name(ph) == name and ph.get("ppw")]
        # ступени, где лимит не соблюдался («!»), в «лучший» не берём:
        # там PPW снят не на заявленной мощности
        held = [c for c in cands if c[1].get("limit_held") is not False]
        best = max(held or cands, key=lambda c: c[1]["ppw"], default=None)
        if best:
            note = "" if held else " (лимит нигде не соблюдался)"
            L.append(f"  {name}: {fmt_si(best[1]['ppw'], 'ед/с на Вт')} "
                     f"при лимите {best[0]:g} Вт{note}")
        else:
            L.append(f"  {name}: PPW не измерен (нет датчика мощности)")
    L.append(BAR)
    L.extend(_legend())
    return "\n".join(L)


SWEEP_CSV_FIELDS = ("limit_w", "applied_w", "phase", "threads", "work_per_s",
                    "power_w", "pkg_w", "ppw", "freq_mhz", "limit_held", "note")


def sweep_rows(steps: list[dict]) -> list[dict]:
    rows = []
    for st in steps:
        if st.get("skipped"):
            row = dict.fromkeys(SWEEP_CSV_FIELDS, "")
            row.update(limit_w=f"{st['target']:g}", note=f"skipped: {st['skipped']}")
            rows.append(row)
            continue
        for ph in st["phases"]:
            freq = ph.get("freq") or {}
            held = ph.get("limit_held")
            rows.append({
                "limit_w": f"{st['target']:g}",
                "applied_w": f"{st['applied']:g}",
                "phase": phase_name(ph),
                "threads": ph["threads"],
                "work_per_s": f"{ph['work_per_s']:.0f}",
                "power_w": f"{ph['power_w']:.3f}" if ph.get("power_w") else "",
                "pkg_w": f"{ph['pkg_w']:.3f}" if ph.get("pkg_w") else "",
                "ppw": f"{ph['ppw']:.0f}" if ph.get("ppw") else "",
                "freq_mhz": f"{freq['mhz']:.0f}" if freq.get("mhz") else "",
                "limit_held": "" if held is None else ("yes" if held else "no"),
                "note": st.get("note") or "",
            })
    return rows


def save_sweep_csv(steps: list[dict], path: Path) -> Path | None:
    """Таблица ступеней для графиков (Excel, LibreOffice, pandas). None — не удалось."""
    encoding = "utf-8-sig" if util.IS_WINDOWS else "utf-8"
    try:
        with open(path, "w", newline="", encoding=encoding) as f:
            w = csv.DictWriter(f, fieldnames=SWEEP_CSV_FIELDS)
            w.writeheader()
            w.writerows(sweep_rows(steps))
    except OSError as exc:
        print(f"  ! Не удалось сохранить CSV {path}: {exc}")
        return None
    _give_back_to_sudo_user(path)
    return path


def _header(meta: dict, phases: list[dict], source: str) -> list[str]:
    L: list[str] = []
    bar, dash = BAR, DASH
    L.append(bar)
    L.append("  PPW BENCH — производительность на ватт")
    L.append(bar)
    L.append(f"Дата              : {meta['date']}")
    L.append(f"Процессор         : {meta['cpu']}")
    L.append(f"Ядра / потоки     : {meta['cores']} / {meta['threads']}")
    L.append(f"ОС                : {meta['os']}")
    L.append(f"Питание           : {meta['power']}")
    if meta.get("profile"):
        L.append(f"Профиль питания   : {meta['profile']}")
    L.append(f"Источник мощности : {source}")
    L.append(f"Длительность      : {meta['duration']:.0f} с на фазу (прогрев {meta['warmup']:.0f} с)")
    if meta.get("sweep"):
        L.append(f"Лимиты мощности   : {meta['sweep']}")
        if meta.get("settle"):
            L.append(f"Пауза на ступени  : {meta['settle']:.0f} с после смены лимита")
    if meta.get("repeat", 1) > 1:
        L.append(f"Повторов          : {meta['repeat']} (в фазах — медианы)")
    if meta.get("cooldown") and (len(phases) > 1 or meta.get("repeat", 1) > 1):
        L.append(f"Пауза между фазами: {meta['cooldown']:.0f} с")
    build = next((ph.get("build") for ph in phases if ph.get("build")), None)
    if build:
        L.append(f"Сборка bench      : {build}")
    if meta.get("version"):
        L.append(f"Версия ppw-bench  : {meta['version']}")
    if meta.get("warnings"):
        L.append(dash)
        L.append("ВНИМАНИЕ")
        for w in meta["warnings"]:
            L.append(f"  ! {w}")
    L.append(dash)
    return L


def _phase_lines(ph: dict, basis: str | None) -> list[str]:
    """Подробности одной фазы (без заголовка и разделителя)."""
    L: list[str] = []
    L.append(f"  Производительность  : {fmt_si(ph['work_per_s'], 'ед/с')}")
    L.append(f"  Работа за тест      : {fmt_si(ph['total_work'], 'ед')}")
    if basis == "manual":
        L.append(f"  Мощность (вручную)  : {ph['power_w']:.2f} Вт")
    if ph["pkg_w"]:
        L.append(f"  Ср. мощность CPU    : {ph['pkg_w']:.2f} Вт (пакет)")
    if ph["pp0_w"]:
        L.append(f"  Ср. мощность ядер   : {ph['pp0_w']:.2f} Вт (pp0)")
    if ph["sys_w"]:
        L.append(f"  Ср. мощность системы: {ph['sys_w']:.2f} Вт")
    if ph["energy_j"] and basis:
        L.append(f"  {'Энергия (' + ENERGY_TAGS[basis] + ')':<20}: {ph['energy_j']:.0f} Дж")
    if basis == "manual":
        L.append(f"  PPW (вручную)       : {fmt_si(ph['ppw'], 'ед/с на Вт')}")
    if ph["ppw_pkg"]:
        L.append(f"  PPW (пакет)         : {fmt_si(ph['ppw_pkg'], 'ед/с на Вт')}")
    if ph["ppw_sys"]:
        L.append(f"  PPW (система)       : {fmt_si(ph['ppw_sys'], 'ед/с на Вт')}")
    stab = []
    if ph["pkg_cv"] is not None:
        stab.append(f"мощность ±{ph['pkg_cv']:.1f}%")
    if ph["rate_cv"] is not None:
        stab.append(f"скорость ±{ph['rate_cv']:.1f}%")
    if stab:
        L.append(f"  Стабильность        : {', '.join(stab)}")
    freq_line = format_freq(ph.get("freq"), single=ph["threads"] == 1)
    if freq_line:
        L.append(f"  {'Частота ядра' if ph['threads'] == 1 else 'Частота ядер':<20}: {freq_line}")
    L.append(f"  Окно измерения      : {ph['steady']:.1f} с, {ph['samples']} замеров")
    if ph.get("pin_label"):
        L.append(f"  Ядра                : {ph['pin_label']}")
    if ph.get("repeat", 1) > 1:
        is_ppw = ph["runs_metric"] == "ppw"
        series = _fmt_series(ph["runs_values"], "ед/с на Вт" if is_ppw else "ед/с")
        spread = f", разброс ±{ph['runs_cv']:.1f}%" if ph.get("runs_cv") is not None else ""
        L.append(f"  {'Повторы (' + str(ph['repeat']) + ')':<20}: "
                 f"{'PPW' if is_ppw else 'скорость'} {series}{spread}")
    return L


def format_freq(freq: dict | None, single: bool) -> str | None:
    """{"mhz", "cpu"?, "tiers"?} -> "1.12 ГГц (CPU 4)" / "3.10 ГГц в среднем; P 3.40, E 2.60 ГГц"."""
    if not freq or not freq.get("mhz"):
        return None
    text = f"{freq['mhz'] / 1000:.2f} ГГц"
    if single:
        if freq.get("cpu") is not None:
            text += f" (CPU {freq['cpu']})"
        return text
    tiers = freq.get("tiers")
    if tiers:
        parts = [f"{TIER_NAMES[t]} {tiers[t] / 1000:.2f}" for t in TIER_ORDER if tiers.get(t)]
        text += " в среднем; " + ", ".join(parts) + " ГГц"
    return text


def _fmt_series(vals: list[float], unit: str) -> str:
    """[9.6e7, 9.5e7] -> "96.4 / 95.1 Mед/с на Вт" (одна приставка на все)."""
    top = max(abs(v) for v in vals)
    for div, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, "")):
        if top >= div:
            break
    nums = " / ".join(f"{v / div:.1f}" for v in vals)
    return f"{nums} {prefix}{unit}"


def _give_back_to_sudo_user(path: Path) -> None:
    """Под sudo файл создаётся от root — возвращаем его пользователю."""
    if util.IS_WINDOWS or os.geteuid() != 0:
        return
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid:
        try:
            os.chown(path, int(uid), int(gid))
        except (OSError, ValueError):
            pass


def save_report(report: str, out_arg: str | None) -> Path:
    if out_arg:
        out = Path(out_arg)
    else:
        out = util.DATA_DIR / f"ppw-report-{datetime.now():%Y%m%d-%H%M%S}.txt"
    if out.suffix.lower() != ".txt":
        out = out.with_suffix(".txt")

    # UTF-8 с BOM нужен Блокноту на Windows; на Linux BOM только мешает.
    encoding = "utf-8-sig" if util.IS_WINDOWS else "utf-8"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report + "\n", encoding=encoding)
    except OSError as exc:
        if out_arg:
            raise SystemExit(f"Не удалось сохранить отчёт: {out} ({exc})") from exc
        # Папка с программой может быть доступна только для чтения — пишем в текущую.
        fallback = Path.cwd() / out.name
        try:
            fallback.write_text(report + "\n", encoding=encoding)
        except OSError as exc2:
            raise SystemExit(
                f"Не удалось сохранить отчёт: {out} ({exc}); {fallback} ({exc2})"
            ) from exc2
        out = fallback
    _give_back_to_sudo_user(out)
    return out
