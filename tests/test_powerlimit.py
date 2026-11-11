# -*- coding: utf-8 -*-
"""Ступенчатый замер мощности: лимиты powercap, прогон по ступеням, отчёт."""

import sys

import pytest

from ppwbench import cli, powerlimit, report

UW = 1_000_000
# зоны powercap называются intel-rapl:0, а ':' в именах файлов Windows недопустим
sysfs_only = pytest.mark.skipif(sys.platform == "win32", reason="фейковый sysfs: нужен ':' в имени")


def _zone(root, name, label, pl1=28, pl2=64, max_uw=0, enabled=1):
    z = root / name
    z.mkdir(parents=True)
    (z / "name").write_text(label + "\n")
    (z / "enabled").write_text(f"{enabled}\n")
    for idx, (cname, w) in enumerate((("long_term", pl1), ("short_term", pl2))):
        (z / f"constraint_{idx}_name").write_text(cname + "\n")
        (z / f"constraint_{idx}_power_limit_uw").write_text(f"{w * UW}\n")
        (z / f"constraint_{idx}_max_power_uw").write_text(f"{max_uw}\n")
    return z


def _limit(z, idx):
    return int((z / f"constraint_{idx}_power_limit_uw").read_text())


@pytest.fixture
def powercap(tmp_path):
    msr = _zone(tmp_path, "intel-rapl:0", "package-0")
    mmio = _zone(tmp_path, "intel-rapl-mmio:0", "package-0", pl1=45, pl2=90, enabled=0)
    core = tmp_path / "intel-rapl:0:0"  # подзона core — не трогаем
    core.mkdir()
    (core / "name").write_text("core\n")
    return tmp_path, msr, mmio


def test_parse_steps():
    assert powerlimit.parse_steps("5, 8,10W;15 вт,8") == [5, 8, 10, 15]
    for bad in ("", "abc", "1", "500"):
        with pytest.raises(ValueError):
            powerlimit.parse_steps(bad)


@sysfs_only
def test_discovers_both_interfaces(powercap):
    root, msr, mmio = powercap
    lim = powerlimit.PowercapLimiter(root)
    assert lim.zones == [mmio, msr] or set(lim.zones) == {msr, mmio}
    assert sorted(c.pl for c in lim.constraints) == ["PL1", "PL1", "PL2", "PL2"]
    assert lim.problem() is None


@sysfs_only
def test_apply_and_restore(powercap):
    root, msr, mmio = powercap
    lim = powerlimit.PowercapLimiter(root)
    lim.save()
    assert lim.apply(15) == 15
    for z in (msr, mmio):
        assert _limit(z, 0) == _limit(z, 1) == 15 * UW
    assert (mmio / "enabled").read_text().strip() == "1"
    assert lim.verify() == []
    (msr / "constraint_0_power_limit_uw").write_text(f"{20 * UW}\n")  # thermald вмешался
    assert lim.verify() == ["intel-rapl:0 PL1: 15 → 20 Вт"]
    assert lim.restore() == []
    assert (_limit(msr, 0), _limit(msr, 1)) == (28 * UW, 64 * UW)
    assert (_limit(mmio, 0), _limit(mmio, 1)) == (45 * UW, 90 * UW)
    assert (mmio / "enabled").read_text().strip() == "0"


@sysfs_only
def test_apply_clamps_to_max(tmp_path):
    _zone(tmp_path, "intel-rapl:0", "package-0", max_uw=30 * UW)
    lim = powerlimit.PowercapLimiter(tmp_path)
    assert lim.apply(45) == 30


@sysfs_only
def test_locked_limit_raises(powercap, monkeypatch):
    root, msr, _ = powercap
    lim = powerlimit.PowercapLimiter(root)
    # BIOS-lock: запись «проходит», но значение не меняется
    monkeypatch.setattr(powerlimit.PowercapLimiter, "_write", staticmethod(lambda p, v: None))
    with pytest.raises(powerlimit.LimitError, match="заблокирован"):
        lim.apply(10)


@sysfs_only
def test_problems(tmp_path):
    assert "нет лимитов" in powerlimit.PowercapLimiter(tmp_path).problem()
    _zone(tmp_path, "intel-rapl:0", "package-0")
    amd = powerlimit.PowercapLimiter(tmp_path, vendor="AuthenticAMD")
    assert "AMD" in amd.problem()


def test_limit_held():
    assert powerlimit.limit_held(10, 10.9) is True
    assert powerlimit.limit_held(10, 12.0) is False
    assert powerlimit.limit_held(10, None) is None


def _phase(threads, work, power, kind=None):
    return {"threads": threads, "core_kind": kind, "work_per_s": work, "power_w": power,
            "pkg_w": power, "ppw": work / power, "freq": {"mhz": 2000.0}}


@sysfs_only
def test_run_sweep_restores_and_reports(powercap, capsys):
    root, msr, _ = powercap
    lim = powerlimit.PowercapLimiter(root)
    seen = []

    def run_plan():
        seen.append(_limit(msr, 0))
        w = seen[-1] / UW
        return [_phase(8, 1e9 * w ** 0.5, w + (5 if w == 10 else 0))]

    warnings: list[str] = []
    steps = cli.run_sweep(lim, [5, 10], 0, run_plan, warnings)
    assert seen == [5 * UW, 10 * UW]
    assert _limit(msr, 0) == 28 * UW  # вернули
    assert [s["phases"][0]["limit_held"] for s in steps] == [True, False]
    assert "возвращены" in capsys.readouterr().out

    meta = {"date": "d", "cpu": "c", "cores": 4, "threads": 8, "os": "o", "power": "p",
            "duration": 20, "warmup": 8, "warnings": warnings, "sweep": lim.describe(),
            "settle": 0}
    text = report.build_sweep_report(meta, steps, "src")
    assert "ПО СТУПЕНЯМ МОЩНОСТИ" in text and "при лимите 5 Вт" in text
    assert "15.0 Вт !" in text
    rows = report.sweep_rows(steps)
    assert [r["limit_held"] for r in rows] == ["yes", "no"]


@sysfs_only
def test_run_sweep_restores_on_interrupt(powercap):
    root, msr, _ = powercap
    lim = powerlimit.PowercapLimiter(root)

    def run_plan():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cli.run_sweep(lim, [5], 0, run_plan, [])
    assert _limit(msr, 0) == 28 * UW


@sysfs_only
def test_run_sweep_stops_on_limit_error(powercap):
    root, _, _ = powercap
    lim = powerlimit.PowercapLimiter(root)

    def boom(_watts):
        raise powerlimit.LimitError("заблокирован")

    lim.apply = boom
    warnings: list[str] = []
    assert cli.run_sweep(lim, [5, 10], 0, lambda: [], warnings) == []
    assert "остановлен на 5 Вт" in warnings[0]


def test_menu_sweep_step(monkeypatch):
    class Lim:
        mode = "auto"

        def problem(self):
            return None

        def describe(self):
            return "fake"

    class FakeReader:
        pass

    answers = iter(["2", "1", "3", "7, 12", ""])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    res = cli.interactive_menu(FakeReader(), {"p": [0], "e": [1]}, 10, Lim())
    assert res["sweep"] == [7, 12] and res["no_st"]


def test_manual_limiter_without_console():
    def eof(_prompt):
        raise EOFError

    lim = powerlimit.ManualLimiter(ask=eof)
    warnings: list[str] = []
    assert cli.run_sweep(lim, [10], 0, lambda: [], warnings) == []
    assert "нужна консоль" in warnings[0]
