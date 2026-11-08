import argparse

import pytest

from ppwbench import cli, core, cpusets
from ppwbench.report import build_report


def test_cpulist_roundtrip():
    assert cpusets.parse_cpulist("0-3,8,10-11") == [0, 1, 2, 3, 8, 10, 11]
    assert cpusets.format_cpulist([11, 0, 1, 2, 3, 8, 10]) == "0-3,8,10-11"
    with pytest.raises(ValueError):
        cpusets.parse_cpulist("5-2")


def _mtl_raw(eff_e: int, eff_lpe: int) -> bytes:
    """Раскладка 185H, как её нумерует Windows: P 0-1,10-19; E 2-9; LP-E 20-21.
    У LP-E свой кэш последнего уровня (LLC 1), у P и E общий L3 (LLC 0)."""
    p = [0, 1] + list(range(10, 20))
    entries = [cpusets.build_cpu_set_entry(i, 2 if eff_e < 2 else 3, llc=0) for i in p]
    entries += [cpusets.build_cpu_set_entry(i, eff_e, llc=0) for i in range(2, 10)]
    entries += [cpusets.build_cpu_set_entry(i, eff_lpe, llc=1) for i in (20, 21)]
    entries += [cpusets.build_cpu_set_entry(0, 2, group=1)]  # группа 1 игнорируется
    return b"".join(entries)


MTL = {"p": [0, 1] + list(range(10, 20)), "e": list(range(2, 10)), "lpe": [20, 21]}


def test_windows_meteor_lake_e_and_lpe_share_class():
    # E и LP-E в одном классе — различаем по кэшу последнего уровня
    info = cpusets.parse_cpu_set_information(_mtl_raw(eff_e=1, eff_lpe=1))
    assert len(info) == 22
    assert cpusets.windows_tiers(info, intel=True) == MTL


def test_windows_three_efficiency_classes():
    info = cpusets.parse_cpu_set_information(_mtl_raw(eff_e=1, eff_lpe=0))
    assert cpusets.windows_tiers(info, intel=True) == MTL


def test_windows_amd_separate_l3_is_not_lpe():
    # Zen 5 + Zen 5c: у компактных ядер свой L3, но это не LP-E
    raw = b"".join([cpusets.build_cpu_set_entry(i, 1, llc=0) for i in range(8)]
                   + [cpusets.build_cpu_set_entry(i, 0, llc=1) for i in range(8, 24)])
    info = cpusets.parse_cpu_set_information(raw)
    assert cpusets.windows_tiers(info, intel=False) == {"p": list(range(8)),
                                                        "e": list(range(8, 24))}


def test_lunar_lake_like_only_lpe_is_reported_as_e():
    # все «не-P» ядра без общего L3 — не делим, это просто E
    tiers = cpusets.classify_tiers({0: 1, 1: 1, 2: 0, 3: 0}, lpe_candidates={2, 3})
    assert tiers == {"p": [0, 1], "e": [2, 3]}


def test_non_hybrid_has_no_types():
    raw = b"".join(cpusets.build_cpu_set_entry(i, 0) for i in range(8))
    info = cpusets.parse_cpu_set_information(raw)
    assert cpusets.windows_tiers(info, intel=True) is None


class FakeBackend:
    def __init__(self, types):
        self.types = types

    def core_types(self):
        return self.types


def test_resolve_pin_modes():
    hybrid = FakeBackend({"p": [0, 1, 2, 3], "e": [4, 5, 6, 7]})
    cpus, label, warns = cli.resolve_pin("auto", hybrid)
    assert cpus == [0, 1, 2, 3] and "P-ядра" in label and not warns
    assert cli.resolve_pin("e", hybrid)[0] == [4, 5, 6, 7]
    assert cli.resolve_pin("none", hybrid)[0] is None
    assert cli.resolve_pin("0,2", hybrid)[0] == [0, 2]

    flat = FakeBackend(None)
    assert cli.resolve_pin("auto", flat) == (None, "без закрепления (CPU не гибридный)", [])
    cpus, _, warns = cli.resolve_pin("p", flat)
    assert cpus is None and warns


def test_resolve_st_all_tiers():
    phases, warns = cli.resolve_st("all", FakeBackend(MTL))
    assert not warns
    assert [p["tag"] for p in phases] == ["ST-P", "ST-E", "ST-LP-E"]
    assert [p["core_kind"] for p in phases] == ["P", "E", "LP-E"]
    assert phases[2]["cpus"] == [20, 21]
    assert phases[2]["label"] == "LP-E-ядра (CPU 20-21)"

    # без LP-E (Alder Lake): только P и E
    phases, _ = cli.resolve_st("all", FakeBackend({"p": [0, 1], "e": [2, 3]}))
    assert [p["tag"] for p in phases] == ["ST-P", "ST-E"]


def test_resolve_st_lpe_missing_warns():
    phases, warns = cli.resolve_st("lpe", FakeBackend({"p": [0, 1], "e": [2, 3]}))
    assert phases[0]["cpus"] is None
    assert warns and "LP-E" in warns[0] and "есть: P, E" in warns[0]
    phases, warns = cli.resolve_st("lpe", FakeBackend(MTL))
    assert phases[0]["cpus"] == [20, 21] and not warns


def test_pin_arg_validation():
    assert cli.pin_arg("P") == "p"
    assert cli.pin_arg("LPE") == "lpe"
    assert cli.pin_arg("all") == "all"
    assert cli.pin_arg("0-3") == "0-3"
    with pytest.raises(argparse.ArgumentTypeError):
        cli.pin_arg("fast")


def _run(ppw, work):
    return dict(threads=1, duration=20, steady=11.7, total_work=7e9, work_per_s=work,
                build="gcc", pkg_w=work / ppw, pp0_w=None, sys_w=None, pkg_cv=5.0,
                rate_cv=1.0, ppw_pkg=ppw, ppw_sys=None, basis="pkg", power_w=work / ppw,
                ppw=ppw, energy_j=100.0, samples=58, cpus=[0, 1], pin_label="P-ядра (CPU 0-1)")


def test_aggregate_runs_median_and_spread():
    runs = [_run(30e6, 3.0e8), _run(32e6, 3.2e8), _run(10e6, 1.0e8)]  # один выброс
    agg = core.aggregate_runs(runs)
    assert agg["repeat"] == 3
    assert agg["ppw"] == 30e6          # медиана, не среднее
    assert agg["work_per_s"] == 3.0e8
    assert agg["runs_values"] == [30e6, 32e6, 10e6]
    assert agg["runs_cv"] > 30

    two = core.aggregate_runs([_run(30e6, 3e8), _run(32e6, 3.2e8)])
    assert two["runs_cv"] == pytest.approx(1 / 31 * 100)
    assert core.aggregate_runs([_run(30e6, 3e8)])["repeat"] == 1


def test_report_shows_repeats_and_cores():
    agg = core.aggregate_runs([_run(30e6, 3e8), _run(31e6, 3.1e8), _run(32e6, 3.2e8)])
    meta = dict(date="d", cpu="c", cores=4, threads=8, os="o", power="p",
                duration=20, warmup=8, cooldown=10, repeat=3, warnings=[])
    text = build_report(meta, [agg], "src")
    assert "Повторов          : 3" in text
    assert "Повторы (3)         : PPW 30.0 / 31.0 / 32.0 Mед/с на Вт, разброс" in text
    assert "Ядра                : P-ядра (CPU 0-1)" in text


def test_report_titles_core_kind():
    st = dict(_run(30e6, 3e8), core_kind="LP-E", pin_label="LP-E-ядра (CPU 20-21)")
    agg = core.aggregate_runs([st])
    meta = dict(date="d", cpu="c", cores=4, threads=8, os="o", power="p",
                duration=20, warmup=8, warnings=[])
    text = build_report(meta, [agg], "src")
    assert "ФАЗА 1 — SINGLE-THREAD, LP-E-ядро (1 поток(ов))" in text
    assert "  ST (LP-E): 30.000 Mед/с на Вт" in text
