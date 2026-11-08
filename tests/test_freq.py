from ppwbench import core
from ppwbench.report import format_freq

TIERS = {"p": [0, 1], "e": [2, 3], "lpe": [4]}


def test_single_thread_probe():
    f = core.summarize_probe([(11, 4950.0)], TIERS, single=True)
    assert f == {"mhz": 4950.0, "cpu": 11}
    assert format_freq(f, single=True) == "4.95 ГГц (CPU 11)"


def test_unknown_cpu_is_not_shown():
    f = core.summarize_probe([(-1, 3000.0)], None, single=True)
    assert f == {"mhz": 3000.0}


def test_multi_thread_average_and_tiers():
    probes = [(0, 3400.0), (1, 3200.0), (2, 2600.0), (3, 2400.0), (4, 1900.0)]
    f = core.summarize_probe(probes, TIERS, single=False)
    assert f["mhz"] == (3400 + 3200 + 2600 + 2400 + 1900) / 5
    assert f["tiers"] == {"p": 3300.0, "e": 2500.0, "lpe": 1900.0}
    assert format_freq(f, single=False) == "2.70 ГГц в среднем; P 3.30, E 2.50, LP-E 1.90 ГГц"


def test_no_probe_data():
    assert core.summarize_probe([], TIERS, single=True) is None
    assert core.summarize_probe([(0, 0.0)], TIERS, single=True) is None
    assert format_freq(None, single=True) is None


def test_aggregate_runs_freq_median():
    base = dict(threads=1, duration=20, steady=10, total_work=1, work_per_s=1e8, build=None,
                pkg_w=10.0, pp0_w=None, sys_w=None, pkg_cv=None, rate_cv=None,
                ppw_pkg=1e7, ppw_sys=None, basis="pkg", power_w=10.0, ppw=1e7,
                energy_j=100.0, samples=50)
    runs = [dict(base, freq={"mhz": m, "cpu": 4}) for m in (1100.0, 3000.0, 1200.0)]
    assert core.aggregate_runs(runs)["freq"]["mhz"] == 1200.0
