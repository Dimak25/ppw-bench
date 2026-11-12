# -*- coding: utf-8 -*-
"""Пути, консоль, ввод и форматирование — общее для всех платформ."""

from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"

_PKG_DIR = Path(__file__).resolve().parent

# PyInstaller выставляет sys.frozen; Nuitka кладёт __compiled__ в globals модуля.
PYINSTALLER = bool(getattr(sys, "frozen", False))
NUITKA = "__compiled__" in globals()
FROZEN = PYINSTALLER or NUITKA


def _resource_dir() -> Path:
    if PYINSTALLER:
        return Path(getattr(sys, "_MEIPASS", _PKG_DIR.parent))
    # исходники и Nuitka: bench лежит на уровень выше пакета
    return _PKG_DIR.parent


def _data_dir() -> Path:
    """Куда по умолчанию сохранять отчёты: рядом с exe / бинарником."""
    if PYINSTALLER:
        return Path(sys.executable).resolve().parent
    if NUITKA:
        compiled = globals().get("__compiled__")
        containing = getattr(compiled, "containing_dir", None)
        if containing:
            return Path(containing)
        return Path(sys.argv[0]).resolve().parent
    return _PKG_DIR.parent


RESOURCE_DIR = _resource_dir()
DATA_DIR = _data_dir()
BENCH_C = RESOURCE_DIR / "bench.c"
BENCH_EXE = RESOURCE_DIR / ("bench.exe" if IS_WINDOWS else "bench")


def setup_console() -> None:
    if IS_WINDOWS:
        try:
            import ctypes

            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k32.SetConsoleOutputCP(65001)
            k32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def is_interactive() -> bool:
    """Меню показываем только при запуске без аргументов из настоящей консоли."""
    try:
        return len(sys.argv) == 1 and sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def stdin_is_tty() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def ask_choice(prompt: str, choices: set[str], default: str) -> str:
    while True:
        try:
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not raw:
            return default
        if raw in choices:
            return raw
        print(f"Нет такого варианта: {raw}")


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        raw = input(f"{prompt} {hint}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not raw:
        return default
    return raw in ("y", "yes", "д", "да")


def pause(message: str = "Нажмите Enter, чтобы выйти...") -> None:
    try:
        input(message)
    except (EOFError, KeyboardInterrupt):
        print()


def fmt_si(value: float | None, unit: str, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    a = abs(value)
    if a >= 1e12:
        return f"{value / 1e12:.{digits}f} T{unit}"
    if a >= 1e9:
        return f"{value / 1e9:.{digits}f} G{unit}"
    if a >= 1e6:
        return f"{value / 1e6:.{digits}f} M{unit}"
    if a >= 1e3:
        return f"{value / 1e3:.{digits}f} k{unit}"
    return f"{value:.{digits}f} {unit}"
