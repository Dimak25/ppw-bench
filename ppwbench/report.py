# -*- coding: utf-8 -*-
"""Текстовый отчёт и его сохранение."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from ppwbench import util
from ppwbench.core import BASIS_LABELS
from ppwbench.cpusets import TIER_NAMES, TIER_ORDER
from ppwbench.util import fmt_si

ENERGY_TAGS = {"pkg": "пакет", "sys": "система", "manual": "вручную"}


def build_report(meta: dict, phases: list[dict], source: str) -> str:
    L: list[str] = []
    bar = "=" * 66
    dash = "-" * 66
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

    for i, ph in enumerate(phases, 1):
        kind = "MULTI-THREAD" if ph["threads"] > 1 else "SINGLE-THREAD"
        if ph.get("core_kind"):
            kind += f", {ph['core_kind']}-ядро"
        basis = ph.get("basis")
        L.append(f"ФАЗА {i} — {kind} ({ph['threads']} поток(ов))")
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
        L.append(dash)

    L.append("ИТОГ")
    for ph in phases:
        kind = "MT" if ph["threads"] > 1 else "ST"
        if ph.get("core_kind"):
            kind += f" ({ph['core_kind']})"
        if ph.get("ppw"):
            L.append(f"  {kind}: {fmt_si(ph['ppw'], 'ед/с на Вт')} ({BASIS_LABELS[ph['basis']]})")
        else:
            L.append(f"  {kind}: PPW не измерен (нет датчика мощности)")
    L.append(bar)
    L.append("PPW = сколько единиц работы в секунду даёт каждый ватт.")
    L.append("Чем выше — тем эффективнее. Сравнивайте прогоны при одинаковой")
    L.append("длительности, питании от сети и фоновой нагрузке.")
    return "\n".join(L)


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
