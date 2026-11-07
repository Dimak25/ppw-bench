from ppwbench import core
from ppwbench.report import build_report


def test_interp_work():
    pts = [(1000.0, 100.0), (2000.0, 300.0), (3000.0, 600.0)]
    assert core.interp_work(pts, 500) == 100.0
    assert core.interp_work(pts, 1500) == 200.0
    assert core.interp_work(pts, 2500) == 450.0
    assert core.interp_work(pts, 9999) == 600.0
    assert core.interp_work([], 10) == 0.0


def test_warmup_seconds():
    assert core.warmup_seconds(20) == 8.0
    assert core.warmup_seconds(60) == 15.0
    assert core.warmup_seconds(8) == 8 * 0.45


def test_window_power_prefers_energy_counter():
    # сэмплы мощности шумные, а по энергии ровно 10 Дж за 2 с = 5 Вт
    window = [
        {"t": 0.0, "pkg": 1.0, "pkg_j": 100.0},
        {"t": 1.0, "pkg": 9.0, "pkg_j": 104.0},
        {"t": 2.0, "pkg": 2.0, "pkg_j": 110.0},
    ]
    assert core.window_power(window, "pkg") == 5.0
    # без счётчика энергии — среднее по сэмплам
    assert core.window_power([{"t": 0, "sys": 4.0}, {"t": 1, "sys": 6.0}], "sys") == 5.0
    assert core.window_power([], "pkg") is None


def _phase(**kw):
    base = dict(threads=8, duration=20, steady=10.0, total_work=1e9, work_per_s=1e8,
                build="gcc 13", pkg_w=None, pp0_w=None, sys_w=None, pkg_cv=None,
                rate_cv=None, ppw_pkg=None, ppw_sys=None, samples=10)
    base.update(kw)
    return base


def test_basis_system_power_is_not_labeled_package():
    res = core.apply_power_basis(_phase(sys_w=20.0, ppw_sys=5e6), None)
    assert res["basis"] == "sys"
    assert res["ppw"] == 5e6
    meta = dict(date="d", cpu="c", cores=4, threads=8, os="o", power="p",
                duration=20, warmup=8, warnings=[])
    text = build_report(meta, [res], "src")
    assert "PPW (пакет)" not in text
    assert "по мощности системы" in text
    assert "Энергия (система)" in text


def test_basis_priority():
    assert core.apply_power_basis(_phase(pkg_w=10.0, sys_w=20.0), None)["basis"] == "pkg"
    r = core.apply_power_basis(_phase(pkg_w=10.0), 40.0)
    assert r["basis"] == "manual" and r["ppw"] == 1e8 / 40.0
    assert core.apply_power_basis(_phase(), None)["ppw"] is None


def test_coeff_var():
    assert core.coeff_var([1.0, 1.0]) is None
    assert core.coeff_var([1.0, 1.0, 1.0]) == 0.0
