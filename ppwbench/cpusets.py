# -*- coding: utf-8 -*-
"""Списки CPU и типы ядер (P/E) — платформенно-независимые хелперы.

Вынесено отдельно от platforms/windows.py, чтобы разбор структур Windows
можно было тестировать на любой ОС.
"""

from __future__ import annotations

import struct


def parse_cpulist(text: str) -> list[int]:
    """"0-3,8,10-11" -> [0, 1, 2, 3, 8, 10, 11]."""
    cpus: set[int] = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            a, b = int(lo), int(hi)
            if b < a:
                raise ValueError(f"неверный диапазон CPU: {part}")
            cpus.update(range(a, b + 1))
        else:
            cpus.add(int(part))
    return sorted(cpus)


def format_cpulist(cpus: list[int]) -> str:
    """[0, 1, 2, 3, 8, 10, 11] -> "0-3,8,10-11"."""
    out: list[str] = []
    nums = sorted(set(cpus))
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ",".join(out)


TIER_ORDER = ("p", "e", "lpe")
TIER_NAMES = {"p": "P", "e": "E", "lpe": "LP-E"}


def classify_tiers(classes: dict[int, float],
                   lpe_candidates: set[int] | None = None) -> dict[str, list[int]] | None:
    """{cpu: класс производительности} -> {"p": [...], "e": [...], ["lpe": [...]]}.

    P — ядра с наибольшим классом. Если классов три и больше, следующий —
    E, остальные — LP-E. Если классов два, ОС часто не отличает E от LP-E
    (Meteor Lake): тогда LP-E — те из «не-P» ядер, что бэкенд пометил в
    lpe_candidates (у них нет общего с P-ядрами L3: они на SoC-кристалле).
    Делим, только если остаются и E, и LP-E — иначе это просто E-ядра
    (например, у Lunar Lake). Один класс у всех — CPU не гибридный -> None."""
    if not classes:
        return None
    levels = sorted(set(classes.values()), reverse=True)
    if len(levels) < 2:
        return None
    p = sorted(c for c, v in classes.items() if v == levels[0])
    rest = sorted(c for c, v in classes.items() if v != levels[0])
    if len(levels) >= 3:
        return {
            "p": p,
            "e": sorted(c for c in rest if classes[c] == levels[1]),
            "lpe": sorted(c for c in rest if classes[c] != levels[1]),
        }
    if lpe_candidates:
        lpe = [c for c in rest if c in lpe_candidates]
        e = [c for c in rest if c not in lpe_candidates]
        if e and lpe:
            return {"p": p, "e": e, "lpe": lpe}
    return {"p": p, "e": rest}


# SYSTEM_CPU_SET_INFORMATION (GetSystemCpuSetInformation), Type = CpuSetInformation:
#   DWORD Size; DWORD Type; DWORD Id; WORD Group; BYTE LogicalProcessorIndex;
#   BYTE CoreIndex; BYTE LastLevelCacheIndex; BYTE NumaNodeIndex;
#   BYTE EfficiencyClass; BYTE AllFlags; DWORD Reserved; DWORD64 AllocationTag
_CPU_SET = struct.Struct("<IIIHBBBBBBIQ")


def parse_cpu_set_information(raw: bytes) -> dict[int, tuple[int, int]]:
    """Буфер GetSystemCpuSetInformation -> {логический CPU: (EfficiencyClass, LLC)}.

    LLC — LastLevelCacheIndex: номер экземпляра кэша последнего уровня.
    Берём только группу процессоров 0: маска аффинити процесса покрывает
    максимум 64 CPU одной группы, а у ноутбуков их меньше."""
    out: dict[int, tuple[int, int]] = {}
    offset = 0
    while offset + 8 <= len(raw):
        size, kind = struct.unpack_from("<II", raw, offset)
        if size < 8:
            break
        if kind == 0 and offset + _CPU_SET.size <= len(raw):
            (_size, _kind, _id, group, lp, _core, llc, _numa, eff,
             _flags, _res, _tag) = _CPU_SET.unpack_from(raw, offset)
            if group == 0:
                out[lp] = (eff, llc)
        offset += size
    return out


def windows_tiers(info: dict[int, tuple[int, int]], intel: bool) -> dict[str, list[int]] | None:
    """Типы ядер из данных GetSystemCpuSetInformation.

    Кандидаты в LP-E — ядра с другим кэшем последнего уровня, чем у P-ядер.
    Только для Intel: у AMD Zen 5 + Zen 5c разные L3 у обычных компактных
    ядер, и это не LP-E."""
    classes = {c: eff for c, (eff, _llc) in info.items()}
    cands: set[int] = set()
    if intel and classes:
        top = max(classes.values())
        p_llc = {llc for c, (eff, llc) in info.items() if eff == top}
        cands = {c for c, (eff, llc) in info.items() if eff != top and llc not in p_llc}
    return classify_tiers(classes, cands)


def build_cpu_set_entry(lp: int, eff: int, group: int = 0, llc: int = 0) -> bytes:
    """Одна запись SYSTEM_CPU_SET_INFORMATION (для тестов)."""
    return _CPU_SET.pack(_CPU_SET.size, 0, 256 + lp, group, lp, lp // 2, llc, 0, eff, 0, 0, 0)
