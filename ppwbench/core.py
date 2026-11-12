# -*- coding: utf-8 -*-
"""Платформенно-независимая часть: сборка bench, запуск фаз, расчёт PPW."""

from __future__ import annotations

import contextlib
import shutil
import statistics
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
    """Фоновый опрос датчиков мощности."""

    def __init__(self, reader, interval: float = 0.2) -> None:
        super().__init__(daemon=True)
        self.reader = reader
        self.interval = interval
        self.samples: list[dict] = []
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            t = time.perf_counter()
            vals = self.reader.read() or {}
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


def _make_executable(exe: Path) -> None:
    """Распаковщики (Nuitka onefile, zip) не всегда сохраняют бит +x."""
    if util.IS_WINDOWS:
        return
    try:
        mode = exe.stat().st_mode
        if not mode & 0o111:
            exe.chmod(mode | 0o755)
    except OSError:
        pass


def ensure_bench(backend) -> Path:
    exe = util.BENCH_EXE
    if util.FROZEN:
        if exe.exists():
            _make_executable(exe)
            return exe
        raise SystemExit(
            f"В Portable-сборке не найден встроенный {exe.name}.\n"
            "Скачайте релиз заново: https://github.com/Dimak25/ppw-bench/releases"
        )
    src = util.BENCH_C
    if exe.exists() and (not src.exists() or exe.stat().st_mtime >= src.stat().st_mtime):
        _make_executable(exe)
        return exe
    if not src.exists():
        raise SystemExit(f"Не найден исходник: {src}")
    cc = find_compiler(backend.compiler_candidates())
    if not cc and exe.exists():
        # готовый bench из архива релиза: компилятора нет — используем его
        _make_executable(exe)
        return exe
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


def per_second(window: list[dict], key: str, t0: float) -> list[float]:
    """Средние значения по секундным интервалам окна.

    Разброс считается по секундам, как и для скорости: отдельные замеры
    раз в 0,2 с ловят мгновенные всплески (фоновые задачи, переходы
    между ядрами) и завышают «нестабильность», особенно в ST-фазе."""
    buckets: dict[int, list[float]] = {}
    for s in window:
        if s.get(key) is not None:
            buckets.setdefault(int(s["t"] - t0), []).append(s[key])
    return [sum(v) / len(v) for _, v in sorted(buckets.items())]


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


def summarize_probe(probes: list[tuple[int, float]],
                    tiers: dict[str, list[int]] | None, single: bool) -> dict | None:
    """Частота ядер по замеру самого bench (строки FREQ <поток> <cpu> <МГц>).

    bench в конце фазы, не давая ядру остыть, крутит цепочку зависимых
    сложений (1 такт каждое) — это прямой замер тактовой частоты, без
    счётчиков ОС. single — однопоточная фаза: частота и номер CPU.
    Иначе — среднее по потокам и, если известны типы ядер, по P / E / LP-E."""
    probes = [(c, m) for c, m in probes if m and m > 0]
    if not probes:
        return None
    out: dict = {"mhz": sum(m for _c, m in probes) / len(probes)}
    if single:
        if probes[0][0] >= 0:
            out["cpu"] = probes[0][0]
        return out
    tier_of = {c: t for t, lst in (tiers or {}).items() for c in lst}
    groups: dict[str, list[float]] = {}
    for c, m in probes:
        if c in tier_of:
            groups.setdefault(tier_of[c], []).append(m)
    if len(groups) > 1:
        out["tiers"] = {t: sum(v) / len(v) for t, v in groups.items()}
    return out


# Профили нагрузки bench (наборы инструкций). scalar — обычный код общего
# назначения; остальные — узкие нагрузки на конкретные блоки ядра.
PROFILES = ("scalar", "avx2", "aes")
PROFILE_LABELS = {"scalar": "обычный", "avx2": "AVX2+FMA", "aes": "AES-NI"}
PROFILE_DESCS = {
    "scalar": "целочисленный и FP-код общего назначения",
    "avx2": "векторная FP32: 256-битные FMA (наука, рендер, кодеки)",
    "aes": "шифрование AES-128 (диски, VPN, архивы)",
}

_caps_cache: dict[str, list[str]] = {}


def bench_profiles(exe: Path) -> list[str]:
    """Профили, которые bench может выполнить на этом CPU (bench --caps).

    Старый bench без --caps умеет только scalar."""
    key = str(exe)
    if key not in _caps_cache:
        profiles = ["scalar"]
        try:
            out = subprocess.run([key, "--caps"], capture_output=True, text=True,
                                 timeout=10).stdout
            for line in out.splitlines():
                if line.startswith("CAPS "):
                    profiles = [p for p in line.split()[1:] if p in PROFILES] or profiles
        except (OSError, subprocess.SubprocessError):
            pass
        _caps_cache[key] = profiles
    return _caps_cache[key]


def run_phase(exe: Path, threads: int, duration: float, sampler: Sampler,
              backend=None, cpus: list[int] | None = None,
              tiers: dict[str, list[int]] | None = None, profile: str = "scalar") -> dict:
    """Один прогон bench. cpus — ограничить bench этими логическими CPU,
    profile — нагрузка (scalar, avx2, aes)."""
    args = [str(exe), str(threads), f"{duration:.3f}"]
    if profile != "scalar":
        args += ["0", profile]  # 0 — размер внутреннего цикла по умолчанию
    pin_ctx = backend.pinned(cpus) if (backend is not None and cpus) else contextlib.nullcontext()
    with pin_ctx as applied:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            # stderr никто не читает: PIPE мог бы переполниться и подвесить bench
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    assert proc.stdout is not None

    t_start = None
    t_end = None
    build = None
    workload = None
    unit = None  # (название, единиц на одну единицу работы), например ("FLOPS", 192)
    error = None
    probes: list[tuple[int, float]] = []  # (cpu, МГц) из строк FREQ
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
                elif parts[0] == "WORKLOAD" and len(parts) >= 2:
                    workload = parts[1]
                elif parts[0] == "UNIT" and len(parts) >= 3:
                    unit = (parts[1], float(parts[2]))
                elif parts[0] == "ERROR":
                    error = line[6:].strip()
                elif parts[0] == "TICK" and len(parts) >= 3:
                    ticks.append((float(parts[1]), float(parts[2])))
                elif parts[0] == "STOP" and len(parts) >= 3:
                    # нагрузка закончилась; дальше bench меряет частоту
                    t_end = now
                    ticks.append((float(parts[1]), float(parts[2])))
                elif parts[0] == "FREQ" and len(parts) >= 4:
                    probes.append((int(parts[2]), float(parts[3])))
                elif parts[0] == "DONE" and len(parts) >= 3:
                    if t_end is None:  # старый bench без STOP
                        t_end = now
                    ticks.append((float(parts[1]), float(parts[2])))
        proc.wait(timeout=10)
    except BaseException:
        # Ctrl+C, ошибка разбора и т.п.: не оставляем bench грузить CPU
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise

    if error:
        raise RuntimeError(f"bench: {error}")
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
        "workload": workload,
        "profile": profile,
        "unit": unit,
        "pkg_w": pkg,
        "pp0_w": pp0,
        "sys_w": sysw,
        "pkg_cv": coeff_var(per_second(window, "pkg", w_start)),
        "rate_cv": coeff_var(rates),
        "ppw_pkg": work_per_s / pkg if pkg else None,
        "ppw_sys": work_per_s / sysw if sysw else None,
        # заполняются в apply_power_basis()
        "basis": None,
        "power_w": None,
        "ppw": None,
        "energy_j": None,
        "samples": len(window),
        "freq": summarize_probe(probes, tiers, single=(threads == 1)),
        "cpus": applied,
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


MEDIAN_KEYS = (
    "steady", "total_work", "work_per_s", "pkg_w", "pp0_w", "sys_w", "pkg_cv",
    "rate_cv", "ppw_pkg", "ppw_sys", "power_w", "ppw", "energy_j", "samples",
)


def _median(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def aggregate_runs(runs: list[dict]) -> dict:
    """Сводит повторы одной фазы: медианы показателей + разброс между прогонами.

    Медиана устойчива к одному неудачному прогону (фоновая задача, троттлинг),
    а разброс между прогонами показывает, насколько можно доверять разнице
    между машинами."""
    if len(runs) == 1:
        res = dict(runs[0])
        res["repeat"] = 1
        return res
    res = dict(runs[0])
    for key in MEDIAN_KEYS:
        res[key] = _median([r.get(key) for r in runs])
    if res.get("samples") is not None:
        res["samples"] = int(res["samples"])
    freqs = [r["freq"] for r in runs if r.get("freq")]
    if freqs:
        agg = dict(freqs[len(freqs) // 2])
        agg["mhz"] = _median([f["mhz"] for f in freqs])
        tiers = {t for f in freqs for t in f.get("tiers", {})}
        if tiers:
            agg["tiers"] = {t: _median([f.get("tiers", {}).get(t) for f in freqs]) for t in tiers}
        res["freq"] = agg
    metric = "ppw" if all(r.get("ppw") for r in runs) else "work_per_s"
    vals = [r[metric] for r in runs]
    res["repeat"] = len(runs)
    res["runs_metric"] = metric
    res["runs_values"] = vals
    res["runs_cv"] = coeff_var(vals) if len(vals) >= 3 else _spread_pct(vals)
    return res


def _spread_pct(vals: list[float]) -> float | None:
    """Для двух прогонов CV не информативен — берём половину размаха, %."""
    m = sum(vals) / len(vals)
    if m <= 0:
        return None
    return (max(vals) - min(vals)) / 2 / m * 100.0


def cooldown(seconds: float) -> None:
    """Пауза между фазами: ST не должна стартовать на горячем после MT чипе."""
    if seconds <= 0:
        return
    print(f"     пауза {seconds:.0f} с на охлаждение ...", flush=True)
    time.sleep(seconds)
