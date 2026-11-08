import os
import sys

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("Linux backend", allow_module_level=True)

from ppwbench.platforms import linux  # noqa: E402


def _zone(root, name, label, energy, max_range=1_000_000_000):
    z = root / "class/powercap" / name
    z.mkdir(parents=True)
    (z / "name").write_text(label + "\n")
    (z / "energy_uj").write_text(f"{energy}\n")
    (z / "max_energy_range_uj").write_text(f"{max_range}\n")
    return z


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    monkeypatch.setattr(linux, "SYSFS", tmp_path)
    monkeypatch.setattr(linux, "PROCFS", tmp_path / "proc")
    return tmp_path


def test_zone_discovery_skips_mmio_duplicate(sysfs):
    _zone(sysfs, "intel-rapl:0", "package-0", 0)
    _zone(sysfs, "intel-rapl:0:0", "core", 0)
    _zone(sysfs, "intel-rapl:0:1", "uncore", 0)
    _zone(sysfs, "intel-rapl:1", "psys", 0)
    _zone(sysfs, "intel-rapl-mmio:0", "package-0", 0)
    groups = linux.discover_zones()
    assert [z.zone.name for z in groups["pkg"]] == ["intel-rapl:0"]
    assert [z.zone.name for z in groups["pp0"]] == ["intel-rapl:0:0"]
    assert [z.zone.name for z in groups["psys"]] == ["intel-rapl:1"]


def test_energy_counter_wraparound(sysfs):
    z = _zone(sysfs, "intel-rapl:0", "package-0", 999_000_000, max_range=1_000_000_000)
    c = linux.EnergyCounter(z)
    assert c.joules() == 0.0
    (z / "energy_uj").write_text("1000000\n")  # переполнение: +1 Дж до границы, +1 Дж после
    assert c.joules() == pytest.approx(2.0)


def test_reader_reports_energy_and_power(sysfs, monkeypatch):
    z = _zone(sysfs, "intel-rapl:0", "package-0", 0)
    clock = iter([10.0, 12.0])
    monkeypatch.setattr(linux.time, "perf_counter", lambda: next(clock))
    r = linux.PowerReader()
    assert r.kind() == "pkg"
    r.read()
    (z / "energy_uj").write_text("30000000\n")  # 30 Дж за 2 с
    vals = r.read()
    assert vals["pkg_j"] == pytest.approx(30.0)
    assert vals["pkg"] == pytest.approx(15.0)


@pytest.mark.skipif(os.geteuid() == 0, reason="root читает файлы с любыми правами")
def test_unreadable_rapl_suggests_udev(sysfs):
    z = _zone(sysfs, "intel-rapl:0", "package-0", 0)
    (z / "energy_uj").chmod(0o000)
    r = linux.PowerReader()
    assert r.kind() is None
    assert any("udev" in h for h in r.hints())


def test_battery_power_and_ac(sysfs):
    bat = sysfs / "class/power_supply/BAT0"
    bat.mkdir(parents=True)
    (bat / "type").write_text("Battery\n")
    (bat / "status").write_text("Discharging\n")
    (bat / "power_now").write_text("12500000\n")
    ac = sysfs / "class/power_supply/AC"
    ac.mkdir(parents=True)
    (ac / "type").write_text("Mains\n")
    (ac / "online").write_text("0\n")
    assert linux.battery_power_w() == pytest.approx(12.5)
    assert linux.ac_line_status() == 0
    (bat / "status").write_text("Charging\n")
    (ac / "online").write_text("1\n")
    assert linux.battery_power_w() is None
    assert linux.ac_line_status() == 1


def test_cpu_info(sysfs):
    (sysfs / "proc").mkdir()
    (sysfs / "proc/cpuinfo").write_text("processor\t: 0\nmodel name\t: Test CPU 9000\n")
    for cpu, core in ((0, 0), (1, 0), (2, 1), (3, 1)):
        t = sysfs / f"devices/system/cpu/cpu{cpu}/topology"
        t.mkdir(parents=True)
        (t / "physical_package_id").write_text("0\n")
        (t / "core_id").write_text(f"{core}\n")
    assert linux.cpu_name() == "Test CPU 9000"
    assert linux.physical_cores() == 2


def test_amd_core_zone_is_hidden(sysfs):
    (sysfs / "proc").mkdir()
    (sysfs / "proc/cpuinfo").write_text("vendor_id\t: AuthenticAMD\n")
    _zone(sysfs, "intel-rapl:0", "package-0", 0)
    _zone(sysfs, "intel-rapl:0:0", "core", 0)
    groups = linux.discover_zones()
    assert len(groups["pkg"]) == 1
    assert groups["pp0"] == []
