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


def split_by_class(classes: dict[int, float]) -> dict[str, list[int]] | None:
    """{cpu: класс производительности} -> {"p": [...], "e": [...]} или None.

    P — ядра с наибольшим классом, E — все остальные (на Meteor Lake туда
    попадают и E-, и LP-E-ядра). Если класс у всех одинаковый — CPU не гибридный."""
    if not classes:
        return None
    top = max(classes.values())
    p = sorted(c for c, v in classes.items() if v == top)
    e = sorted(c for c, v in classes.items() if v != top)
    if not p or not e:
        return None
    return {"p": p, "e": e}


# SYSTEM_CPU_SET_INFORMATION (GetSystemCpuSetInformation), Type = CpuSetInformation:
#   DWORD Size; DWORD Type; DWORD Id; WORD Group; BYTE LogicalProcessorIndex;
#   BYTE CoreIndex; BYTE LastLevelCacheIndex; BYTE NumaNodeIndex;
#   BYTE EfficiencyClass; BYTE AllFlags; DWORD Reserved; DWORD64 AllocationTag
_CPU_SET = struct.Struct("<IIIHBBBBBBIQ")


def parse_cpu_set_information(raw: bytes) -> dict[int, int]:
    """Буфер GetSystemCpuSetInformation -> {логический CPU: EfficiencyClass}.

    Берём только группу процессоров 0: маска аффинити процесса покрывает
    максимум 64 CPU одной группы, а у ноутбуков их меньше."""
    out: dict[int, int] = {}
    offset = 0
    while offset + 8 <= len(raw):
        size, kind = struct.unpack_from("<II", raw, offset)
        if size < 8:
            break
        if kind == 0 and offset + _CPU_SET.size <= len(raw):
            (_size, _kind, _id, group, lp, _core, _llc, _numa, eff,
             _flags, _res, _tag) = _CPU_SET.unpack_from(raw, offset)
            if group == 0:
                out[lp] = eff
        offset += size
    return out


def build_cpu_set_entry(lp: int, eff: int, group: int = 0) -> bytes:
    """Одна запись SYSTEM_CPU_SET_INFORMATION (для тестов)."""
    return _CPU_SET.pack(_CPU_SET.size, 0, 256 + lp, group, lp, lp // 2, 0, 0, eff, 0, 0, 0)
