from ppwbench import core, cpusets
from ppwbench.report import format_freq

TIERS = {"p": [0, 1], "e": [2, 3], "lpe": [4]}


def _s(freq):
    return {"t": 0.0, "freq": freq}


def test_single_thread_takes_busiest_core():
    window = [
        _s({0: (4800.0, 3.0), 2: (1100.0, 99.0), 3: (3000.0, 10.0)}),
        _s({0: (4800.0, 2.0), 2: (1200.0, 98.0)}),
        _s({0: (4800.0, 1.0), 3: (1150.0, 97.0)}),  # поток перескочил на CPU 3
    ]
    f = core.summarize_freq(window, cpus=[2, 3], tiers=TIERS, single=True)
    assert round(f["mhz"]) == 1150
    assert f["cpu"] == 2  # чаще всего самым загруженным был CPU 2


def test_idle_cores_are_ignored():
    window = [_s({0: (4800.0, 10.0), 1: (4700.0, None)})]
    assert core.summarize_freq(window, cpus=None, tiers=None, single=True) is None


def test_multi_thread_average_and_tiers():
    window = [
        _s({0: (3400.0, 100.0), 1: (3200.0, 100.0), 2: (2600.0, 100.0),
            3: (2400.0, 100.0), 4: (1900.0, 100.0)}),
    ]
    f = core.summarize_freq(window, cpus=None, tiers=TIERS, single=False)
    assert f["mhz"] == (3400 + 3200 + 2600 + 2400 + 1900) / 5
    assert f["tiers"] == {"p": 3300.0, "e": 2500.0, "lpe": 1900.0}
    assert format_freq(f, single=False) == "2.70 ГГц в среднем; P 3.30, E 2.50, LP-E 1.90 ГГц"


def test_format_single():
    assert format_freq({"mhz": 1120.0, "cpu": 4}, single=True) == "1.12 ГГц (CPU 4)"
    assert format_freq(None, single=True) is None


def test_aggregate_runs_freq_median():
    base = dict(threads=1, duration=20, steady=10, total_work=1, work_per_s=1e8, build=None,
                pkg_w=10.0, pp0_w=None, sys_w=None, pkg_cv=None, rate_cv=None,
                ppw_pkg=1e7, ppw_sys=None, basis="pkg", power_w=10.0, ppw=1e7,
                energy_j=100.0, samples=50)
    runs = [dict(base, freq={"mhz": m, "cpu": 4}) for m in (1100.0, 3000.0, 1200.0)]
    assert core.aggregate_runs(runs)["freq"]["mhz"] == 1200.0


def test_pdh_cpu_index():
    assert cpusets.pdh_cpu_index("0,5") == 5
    assert cpusets.pdh_cpu_index("0,_Total") is None
    assert cpusets.pdh_cpu_index("_Total") is None
    assert cpusets.pdh_cpu_index("1,3") is None
