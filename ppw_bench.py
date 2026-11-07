# -*- coding: utf-8 -*-
"""Точка входа PPW Bench (Windows и Linux).

    python3 ppw_bench.py --quick      # Linux
    py ppw_bench.py --quick           # Windows

Код лежит в пакете ppwbench/; этот файл оставлен для привычного запуска
и как вход для сборок Nuitka / PyInstaller.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ppwbench.cli import entry  # noqa: E402

if __name__ == "__main__":
    entry()
