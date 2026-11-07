"""Платформенные бэкенды.

Каждый модуль (windows, linux) предоставляет одинаковый набор:

    cpu_name() -> str
    physical_cores() -> int | None
    logical_cpus() -> int                 # с учётом affinity / cgroups
    ac_line_status() -> int               # 1 сеть, 0 батарея, 255 неизвестно
    power_profile() -> str                # схема / профиль питания
    environment_warnings() -> list[str]
    compiler_candidates() -> list[str]
    PowerReader                           # prime(), read(), kind(), describe(),
                                          # sensor_lines(), hints()

PowerReader.read() возвращает мощности в ваттах ("pkg", "pp0", "sys") и,
если платформа отдаёт счётчики энергии, накопленные джоули ("pkg_j", ...).
По джоулям средняя мощность в окне считается точно, без усреднения сэмплов.
"""

from __future__ import annotations

import sys


def current():
    if sys.platform == "win32":
        from ppwbench.platforms import windows as backend
    elif sys.platform.startswith("linux"):
        from ppwbench.platforms import linux as backend
    else:
        raise SystemExit(f"Платформа {sys.platform} пока не поддерживается (только Windows и Linux).")
    return backend
