# -*- coding: utf-8 -*-
"""Качество замера: урезанные ядра в схеме питания, установившаяся мощность,
PPW по ядрам (pp0)."""

import sys

import pytest

from ppwbench import cli, core, powerlimit, report

# --- схема питания урезает ядра ----------------------------------------------

def test_plan_warnings_hybrid_e_cores_capped_on_ac():
    vals = {"PROCTHROTTLEMAX": (25, 100), "PROCTHROTTLEMAX1": (100, 100),
            "PROCFREQMAX": (0, 0), "PROCFREQMAX1": (0, 0)}
    warns = powerlimit.plan_limit_warnings(vals, on_ac=True, hybrid=True)
    assert len(warns) == 1
    assert "E- и LP-E-ядра (от сети): максимальное состояние 25 %" in warns[0]
    assert "/setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMAX 100" in warns[0]
    # от батареи у этой схемы ограничения нет
    assert powerlimit.plan_limit_warnings(vals, on_ac=False, hybrid=True) == []


def test_plan_warnings_p_core_frequency_cap_on_battery():
    vals = {"PROCTHROTTLEMAX1": (100, 100), "PROCFREQMAX1": (0, 2000)}
    warns = powerlimit.plan_limit_warnings(vals, on_ac=False, hybrid=True)
    assert len(warns) == 1 and "P-ядра (от батареи): максимальная частота 2000 МГц" in warns[0]
    assert "/setdcvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCFREQMAX1 0" in warns[0]


def test_plan_warnings_flat_cpu():
    vals = {"PROCTHROTTLEMAX": (80, 100), "PROCTHROTTLEMAX1": (50, 50)}
    warns = powerlimit.plan_limit_warnings(vals, on_ac=True, hybrid=False)
    # на обычном CPU класс 1 не используется
    assert len(warns) == 1 and "урезает ядра (от сети): максимальное состояние 80 %" in warns[0]


def test_read_plan_settings_skips_missing():
    def run(*args):
        if args[3] == "PROCTHROTTLEMAX":
            return "...: 0x00000000\n...: 0x00000064\n...: 0x00000019\n...: 0x00000064\n"
        if args[3] == "PROCFREQMAX":
            raise powerlimit.LimitError("нет такого параметра")
        return "нет значений"

    assert powerlimit.read_plan_settings(run=run) == {"PROCTHROTTLEMAX": (25, 100)}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux sysfs")
def test_linux_capped_cpus(tmp_path, monkeypatch):
    from ppwbench.platforms import linux
    monkeypatch.setattr(linux, "SYSFS", tmp_path)
    for cpu, hw, cur in ((0, 4800000, 4800000), (1, 4800000, 2000000), (2, 3800000, 3790000)):
        d = tmp_path / f"devices/system/cpu/cpu{cpu}/cpufreq"
        d.mkdir(parents=True)
        (d / "cpuinfo_max_freq").write_text(f"{hw}\n")
        (d / "scaling_max_freq").write_text(f"{cur}\n")
    assert linux.capped_cpus() == [1]


# --- установившаяся мощность --------------------------------------------------

def series(watts):
    return list(enumerate(watts))


def test_settle_time_skips_end_of_boost():
    # 39 Вт буст до 11-й секунды, потом 19 Вт
    w = [39.0] * 11 + [19.0] * 9
    warmup, ok = core.settle_time(series(w), t_min=8, t_max=14)
    assert ok and warmup == 11


def test_settle_time_keeps_minimum_when_stable():
    w = [30.0, 29.5, 30.5] * 7
    assert core.settle_time(series(w), t_min=8, t_max=14) == (8, True)


def test_settle_time_reports_unsettled():
    # мощность «пилит» до самого конца — установившегося уровня нет
    w = [40.0 if i % 2 else 20.0 for i in range(20)]
    assert core.settle_time(series(w), t_min=8, t_max=14) == (8, False)


def test_warmup_arg():
    assert cli.warmup_arg("auto") is None and cli.warmup_arg("12") == 12.0
    with pytest.raises(Exception):
        cli.warmup_arg("-1")


# --- отчёт ------------------------------------------------------------------------

def phase(**kw):
    ph = {"threads": 1, "core_kind": "E", "profile": "scalar", "unit": None,
          "work_per_s": 6.17e7, "total_work": 1.2e9, "basis": "pkg", "power_w": 5.45,
          "pkg_w": 5.45, "pp0_w": 1.33, "sys_w": None, "energy_j": 64, "ppw": 6.17e7 / 5.45,
          "ppw_pkg": 6.17e7 / 5.45, "ppw_pp0": 6.17e7 / 1.33, "ppw_sys": None,
          "pkg_cv": 6.0, "rate_cv": 0.1, "freq": None, "steady": 11.7, "samples": 56,
          "warmup": 8.0, "settled": True}
    ph.update(kw)
    return ph


META = {"date": "d", "cpu": "c", "cores": 4, "threads": 8, "os": "o", "power": "p",
        "duration": 20, "warmup": 8, "warmup_auto": True, "warnings": []}


def test_report_shows_core_ppw_and_auto_warmup():
    text = report.build_report(META, [phase()], "src")
    assert "PPW (ядра, pp0)     : 46.391 Mед/с на Вт" in text
    assert "ST (E): 11.321 Mед/с на Вт (по мощности пакета; по ядрам 46.391 Mед/с на Вт)" in text
    assert "прогрев авто, не меньше 8 с" in text
    assert "нестабильна" not in text


def test_report_flags_unstable_phase():
    text = report.build_report(META, [phase(pkg_cv=37.9, threads=8, core_kind=None)], "src")
    assert "! Мощность нестабильна (±38%)" in text
    assert "MT: " in text and ") !" in text
    assert "! — мощность в окне была нестабильной" in text
    assert report.instability_note(phase(settled=False, pkg_cv=4.0)) is not None


def test_sweep_csv_core_columns():
    rows = report.sweep_rows([{"target": 10, "applied": 10, "phases": [phase()]}])
    assert rows[0]["pp0_w"] == "1.330" and rows[0]["ppw_pp0_rate"] == "46390977"
    assert rows[0]["warmup_s"] == "8.0" and rows[0]["power_cv"] == "6.0"
