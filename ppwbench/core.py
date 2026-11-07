# -*- coding: utf-8 -*-
"""Платформенно-независимая часть: сборка bench, запуск фаз, расчёт PPW."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from ppwbench import util

# --------------------------------------------------------------------------
#  Сбор показаний датчиков в фоне
# --------------------------------------------------------------------------


class Sampler(threading.Thread):
    def __init__(self, reader, interval: float = 0.2) -> None:
        super().__init__(daemon=True)
        self.reader = reader
        self.interval = interval
        self.samples: list[dict[str, float]] = []
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            t = time.perf_counter()
            vals = self.reader.read()
            if vals:
                vals["t"] = t
                self.samples.append(vals)
            if self._stop_event.wait(self.interval):
                break

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=2.0)


# --------------------------------------------------------------------------
#  Сборка C-бенчмарка
# --------------------------------------------------------------------------


def find_compiler(candidates: list[str]) -> str | None:
    for name in ("gcc", "cc", "clang"):
        exe = shutil.which(name)
        if exe:
            return exe
    for cand in candidates:
        if Path(cand).exists():
            return cand
    return None


def compile_commands(cc: str, src: Path, out: Path) -> list[list[str]]:
    """Сначала статическая сборка; если нет статических libc (Fedora без
    glibc-static и т.п.) — обычная динамическая."""
    base = [cc, "-O2", str(src), "-o", str(out)]
    if util.IS_WINDOWS:
        return [base[:2] + ["-static"] + base[2:], base]
    return [base[:2] + ["-static", "-pthread"] + base[2:], base[:2] + ["-pthread"] + base[2:]]


def ensure_bench(backend) -> Path:
    exe = util.BENCH_EXE
    if util.FROZEN:
        if exe.exists():
            return exe
        raise SystemExit(
            f"В Portable-сборке не найден встроенный {exe.name}.\n"
            "Скачайте релиз заново: https://github.com/Dimak25/ppw-bench/releases"
        )
    src = util.BENCH_C
    if exe.exists() and (not src.exists() or exe.stat().st_mtime >= src.stat().st_mtime):
        return exe
    if not src.exists():
        raise SystemExit(f"Не найден исходник: {src}")
    cc = find_compiler(backend.compiler_candidates())
    if not cc:
        if util.IS_WINDOWS:
            hint = "Установите MinGW-w64 / TDM-GCC (или добавьте gcc в PATH)"
        else:
            hint = "Установите gcc: sudo apt install gcc / sudo dnf install gcc / sudo pacman -S gcc"
        raise SystemExit(f"Не найден компилятор C. {hint},\nпосле чего запустите снова.")
    errors = []
    for cmd in compile_commands(cc, src, exe):
        print("Сборка benchmark:", " ".join(cmd))
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return exe
        errors.append(res.stderr or res.stdout)
    raise SystemExit("Ошибка сборки bench.c:\n" + errors[-1])


# --------------------------------------------------------------------------
#  Запуск фазы и расчёт
# --------------------------------------------------------------------------


def interp_work(points: list[tuple[float, float]], elapsed_ms: float) -> float:
    """Линейная интерполяция накопленной работы по времени (мс)."""
    if not points:
        return 0.0
    if elapsed_ms <= points[0][0]:
        return points[0][1]
    if elapsed_ms >= points[-1][0]:
        return points[-1][1]
    lo, hi = 0, len(points) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if points[mid][0] <= elapsed_ms:
            lo = mid
        else:
            hi = mid
    x0, y0 = points[lo]
    x1, y1 = points[hi]
    if x1 <= x0:
        return y1
    return y0 + (y1 - y0) * (elapsed_ms - x0) / (x1 - x0)


def warmup_seconds(duration: float) -> float:
    """Сколько секунд в начале фазы отбрасывается как прогрев (буст PL2)."""
    return min(max(8.0, duration * 0.25), duration * 0.45)


def coeff_var(vals: list[float]) -> float | None:
    """Коэффициент вариации, % (None, если данных мало)."""
    if len(vals) < 3:
        return None
    m = sum(vals) / len(vals)
    if m <= 0:
        return None
    var = sum((v - m) ** 2 for v in vals) / len(vals)
    return (var ** 0.5) / m * 100.0


def window_power(window: list[dict], key: str) -> float | None:
    """Средняя мощность в окне.

    Если платформа отдаёт накопленную энергию (key + "_j", Linux powercap),
    берём ΔE/Δt — это точнее среднего по сэмплам. Иначе — среднее сэмплов."""
    jkey = key + "_j"
    es = [(s["t"], s[jkey]) for s in window if s.get(jkey) is not None]
    if len(es) >= 2 and es[-1][0] > es[0][0]:
        w = (es[-1][1] - es[0][1]) / (es[-1][0] - es[0][0])
        if w > 0:
            return w
    vals = [s[key] for s in window if s.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


def run_phase(exe: Path, threads: int, duration: float, sampler: Sampler) -> dict:
    proc = subprocess.Popen(
        [str(exe), str(threads), f"{duration:.3f}"],
        stdout=subprocess.PIPE,
        # stderr никто не читает: PIPE мог бы переполниться и подвесить bench
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    assert proc.stdout is not None

    t_start = None
    t_end = None
    build = None
    ticks: list[tuple[float, float]] = []  # (elapsed_ms, work)
    line_buf = b""
    try:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            line_buf += chunk
            while b"\n" in line_buf:
                raw, line_buf = line_buf.split(b"\n", 1)
                now = time.perf_counter()
                line = raw.decode("ascii", "replace").strip()
                if not line:
                    continue
                parts = line.split()
                if parts[0] == "START":
                    t_start = now
                elif parts[0] == "BUILD":
                    build = line[6:].strip()
                elif parts[0] == "TICK" and len(parts) >= 3:
                    ticks.append((float(parts[1]), float(parts[2])))
                elif parts[0] == "DONE" and len(parts) >= 3:
                    t_end = now
                    ticks.append((float(parts[1]), float(parts[2])))
        proc.wait(timeout=10)
    except BaseException:
        # Ctrl+C, ошибка разбора и т.п.: не оставляем bench грузить CPU
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise

    if t_start is None or t_end is None or t_end <= t_start:
        raise RuntimeError("бенчмарк не сообщил START/DONE")

    # окно установившегося режима: отбрасываем прогрев и хвост
    warmup = warmup_seconds(duration)
    w_start = t_start + warmup
    w_end = t_end - 0.3
    if w_end <= w_start:
        w_start, w_end = t_start + duration * 0.3, t_end

    work0 = interp_work(ticks, (w_start - t_start) * 1000.0)
    work1 = interp_work(ticks, (w_end - t_start) * 1000.0)
    steady = w_end - w_start
    work_per_s = max(work1 - work0, 0.0) / steady if steady > 0 else 0.0
    total_work = ticks[-1][1] if ticks else 0.0

    # показания датчиков в том же окне
    window = [s for s in sampler.samples if w_start <= s["t"] <= w_end]
    pkg = window_power(window, "pkg")
    pp0 = window_power(window, "pp0")
    sysw = window_power(window, "sys")

    # разброс скорости по секундам внутри окна
    pts = [
        (t_start + e / 1000.0, w)
        for (e, w) in ticks
        if w_start <= t_start + e / 1000.0 <= w_end
    ]
    rates = [
        (wb - wa) / (tb - ta)
        for (ta, wa), (tb, wb) in zip(pts, pts[1:])
        if tb - ta > 0.05
    ]

    return {
        "threads": threads,
        "duration": duration,
        "steady": steady,
        "total_work": total_work,
        "work_per_s": work_per_s,
        "build": build,
        "pkg_w": pkg,
        "pp0_w": pp0,
        "sys_w": sysw,
        "pkg_cv": coeff_var([s["pkg"] for s in window if s.get("pkg") is not None]),
        "rate_cv": coeff_var(rates),
        "ppw_pkg": work_per_s / pkg if pkg else None,
        "ppw_sys": work_per_s / sysw if sysw else None,
        # заполняются в apply_power_basis()
        "basis": None,
        "power_w": None,
        "ppw": None,
        "energy_j": None,
        "samples": len(window),
        "time_s": datetime.now(),
    }


BASIS_LABELS = {
    "pkg": "по мощности пакета",
    "sys": "по мощности системы",
    "manual": "по заданной вручную мощности",
}


def apply_power_basis(res: dict, manual: float | None) -> dict:
    """Выбирает, по какой мощности считается итоговый PPW, и запоминает это.

    Раньше PPW по мощности системы записывался в ppw_pkg и в отчёте
    выглядел как «PPW (пакет)» — теперь источник хранится явно."""
    if manual:
        basis, power = "manual", manual
    elif res["pkg_w"]:
        basis, power = "pkg", res["pkg_w"]
    elif res["sys_w"]:
        basis, power = "sys", res["sys_w"]
    else:
        basis, power = None, None
    res["basis"] = basis
    res["power_w"] = power
    res["ppw"] = res["work_per_s"] / power if power else None
    res["energy_j"] = power * res["steady"] if power else None
    return res


def cooldown(seconds: float) -> None:
    """Пауза между фазами: ST не должна стартовать на горячем после MT чипе."""
    if seconds <= 0:
        return
    print(f"     пауза {seconds:.0f} с на охлаждение ...", flush=True)
    time.sleep(seconds)
