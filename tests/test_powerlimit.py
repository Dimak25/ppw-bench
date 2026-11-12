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


# --- Windows: эмуляция лимита потолком частоты -------------------------------

def _model(cap):
    """Похоже на Core Ultra 9 185H: ~30 Вт без потолка, пол ~14 Вт."""
    if cap == 0:
        return 30.0
    return max(14.0, min(30.0, 30.0 * (cap / 3300) ** 1.2))


class FakeCPU:
    def __init__(self):
        self.cap = 0
        self.calls = 0

    def set_cap(self, mhz):
        self.cap = mhz

    def measure(self):
        self.calls += 1
        return _model(self.cap)


@pytest.mark.parametrize("target", [16, 20, 25])
def test_calibrate_converges(target):
    cpu = FakeCPU()
    cap, power, status = powerlimit.calibrate(target, cpu.set_cap, cpu.measure, 2000, 30.0)
    assert status == "ok" and abs(power / target - 1) <= 0.05
    assert cpu.cap == cap and cpu.calls <= 6


def test_calibrate_below_min_and_above_max():
    cpu = FakeCPU()
    assert powerlimit.calibrate(8, cpu.set_cap, cpu.measure, 2000, 30.0)[2] == "below_min"
    cpu = FakeCPU()
    cap, power, status = powerlimit.calibrate(45, cpu.set_cap, cpu.measure, 2000, 30.0)
    assert (cap, status) == (0, "above_max") and cpu.cap == 0


def test_initial_cap_uses_history():
    assert powerlimit.initial_cap(20, []) == 2000
    start = powerlimit.initial_cap(20, [(0, 30.0), (2200, 18.4)])
    assert 2200 < start < 2600


POWERCFG_QH = """GUID схемы питания: {scheme}  (Экономия энергии)
  Псевдоним GUID: SCHEME_MAX
    GUID параметра питания: 75b0ae3f-bce0-45a7-8c89-c9611c25e100  (Максимальная частота процессора)
      Псевдоним GUID: PROCFREQMAX
      Минимальное возможное значение: 0x00000000
      Максимальное возможное значение: 0xffffffff
      Возможное приращение: 0x00000001
    Текущий индекс параметра питания от сети: 0x{ac:08x}
    Текущий индекс параметра питания от батареи: 0x{dc:08x}
"""

SAVER = "a1841308-3541-4fab-bc81-f71556f20b4a"
BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"


class FakePowercfg:
    """powercfg с несколькими схемами: значения хранятся по GUID схемы."""

    def __init__(self, ac=0, dc=3700):
        self.active = SAVER
        self.schemes = {g: {n: [ac, dc] for n in powerlimit.FREQ_SETTINGS}
                        for g in (SAVER, BALANCED)}
        self.activations: list[str] = []

    @property
    def vals(self):
        return self.schemes[SAVER]

    def _scheme(self, name):
        return self.active if name == "SCHEME_CURRENT" else name

    def __call__(self, *args):
        if args[0] == "/getactivescheme":
            return f"GUID схемы питания: {self.active}  (Экономия энергии)"
        if args[0] == "/qh":
            ac, dc = self.schemes[self._scheme(args[1])][args[3]]
            return POWERCFG_QH.format(scheme=self._scheme(args[1]), ac=ac, dc=dc)
        if args[0] in ("/setacvalueindex", "/setdcvalueindex"):
            idx = 0 if args[0] == "/setacvalueindex" else 1
            self.schemes[self._scheme(args[1])][args[3]][idx] = int(args[4])
        elif args[0] == "/setactive":
            self.active = self._scheme(args[1])
            self.activations.append(self.active)
        return ""


def test_read_freq_caps_parses_last_two_values():
    caps = powerlimit.read_freq_caps(FakePowercfg(ac=0, dc=3700))
    assert caps == {"PROCFREQMAX": (0, 3700), "PROCFREQMAX1": (0, 3700)}


def test_freqcap_limiter_save_set_verify_restore(tmp_path):
    pc = FakePowercfg(ac=0, dc=3700)
    journal = tmp_path / "j.json"
    lim = powerlimit.FreqCapLimiter(run=pc, journal=journal)
    assert lim.problem() is None and lim.mt_only
    lim.save()
    assert journal.exists()  # журнал пишется до первой записи
    lim._set(1650)
    assert pc.vals["PROCFREQMAX"] == [1650, 1650] and pc.activations == [SAVER]
    assert '"applied": 1650' in journal.read_text(encoding="utf-8")
    assert lim.verify() == []
    pc.vals["PROCFREQMAX1"][0] = 2000  # кто-то поменял план
    assert lim.verify() == ["PROCFREQMAX1: 1650 → 2000 МГц"]
    assert lim.restore() == []
    assert pc.vals == {"PROCFREQMAX": [0, 3700], "PROCFREQMAX1": [0, 3700]}
    assert not journal.exists()
    assert lim.restore() == []  # повторный вызов (обработчик консоли + finally) — no-op


def test_freqcap_restore_goes_to_original_scheme(tmp_path):
    """Схему сменили посреди замера: возвращаем значения в исходную схему и
    не переключаем пользователю план обратно."""
    pc = FakePowercfg(ac=0, dc=3700)
    lim = powerlimit.FreqCapLimiter(run=pc, journal=tmp_path / "j.json")
    lim.save()
    lim._set(1200)
    pc.active = BALANCED  # пользователь подключил зарядку / сменил схему
    assert "сменилась" in lim.verify()[0]
    assert lim.restore() == []
    assert pc.schemes[SAVER]["PROCFREQMAX"] == [0, 3700]
    assert pc.schemes[BALANCED]["PROCFREQMAX"] == [0, 3700]  # не тронута
    assert pc.active == BALANCED


def test_recover_pending_restores_after_crash(tmp_path):
    pc = FakePowercfg(ac=0, dc=3700)
    journal = tmp_path / "j.json"
    lim = powerlimit.FreqCapLimiter(run=pc, journal=journal)
    lim.save()
    lim._set(800)  # процесс «убили» здесь: restore() не вызывался
    msgs = powerlimit.recover_pending(run=pc, journal=journal)
    assert "возвращены" in msgs[0]
    assert pc.vals == {"PROCFREQMAX": [0, 3700], "PROCFREQMAX1": [0, 3700]}
    assert not journal.exists()
    assert powerlimit.recover_pending(run=pc, journal=journal) == []


def test_recover_pending_keeps_user_changes(tmp_path):
    pc = FakePowercfg(ac=0, dc=3700)
    journal = tmp_path / "j.json"
    lim = powerlimit.FreqCapLimiter(run=pc, journal=journal)
    lim.save()
    lim._set(800)
    pc.vals["PROCFREQMAX"] = [2500, 2500]  # пользователь уже поправил сам
    msgs = powerlimit.recover_pending(run=pc, journal=journal)
    assert "оставляю как есть" in msgs[0]
    assert pc.vals["PROCFREQMAX"] == [2500, 2500] and not journal.exists()


def test_recover_pending_broken_journal(tmp_path):
    journal = tmp_path / "j.json"
    journal.write_text("{not json", encoding="utf-8")
    assert "повреждённый" in powerlimit.recover_pending(run=FakePowercfg(), journal=journal)[0]
    assert not journal.exists()


def test_console_guard_restores_on_close_only():
    calls = []
    guard = powerlimit._ConsoleGuard(lambda: calls.append(1))
    assert guard.handle(0) is False and calls == []  # Ctrl+C -> KeyboardInterrupt и finally
    assert guard.handle(2) is False and calls == [1]  # закрытие окна
    guard.handle(1)  # Ctrl+Break
    assert calls == [1, 1]


def test_freqcap_limiter_reports_broken_powercfg():
    def broken(*_args):
        raise powerlimit.LimitError("нет powercfg")

    assert "плана электропитания" in powerlimit.FreqCapLimiter(run=broken).problem()


def test_run_sweep_skips_unreachable_step():
    class Lim(powerlimit.ManualLimiter):
        mode = "freqcap"

        def apply(self, watts):
            if watts < 14:
                raise powerlimit.StepSkipped("ниже минимума")
            self.last_note = f"потолок {int(watts * 100)} МГц"
            return watts

        def restore(self):
            return []

    warnings: list[str] = []
    steps = cli.run_sweep(Lim(), [8, 20], 0, lambda: [_phase(8, 2e9, 19.5)], warnings)
    assert steps[0]["skipped"] == "ниже минимума" and steps[0]["phases"] == []
    assert steps[1]["note"] == "потолок 2000 МГц"
    meta = {"date": "d", "cpu": "c", "cores": 4, "threads": 8, "os": "o", "power": "p",
            "duration": 20, "warmup": 8, "warnings": warnings}
    text = report.build_sweep_report(meta, steps, "src")
    assert "8 Вт    пропущена: ниже минимума" in text
    assert "20 Вт → потолок 2000 МГц" in text
    rows = report.sweep_rows(steps)
    assert rows[0]["note"] == "skipped: ниже минимума" and rows[1]["note"] == "потолок 2000 МГц"


def test_menu_offers_auto_or_manual(monkeypatch):
    class Auto:
        mode, mt_only, TOOLS = "freqcap", True, ""

        def problem(self):
            return None

        def describe(self):
            return "auto"

    manual = powerlimit.ManualLimiter()
    answers = iter(["1", "1", "2", "2"])  # весь CPU, 20 с, стандартные ступени, вручную
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    res = cli.interactive_menu(object(), {"p": [0], "e": [1]}, 10, Auto(), 0, manual)
    assert res["limiter"] is manual and res["sweep"] == list(powerlimit.DEFAULT_STEPS)


def test_menu_st_only_falls_back_to_manual(monkeypatch):
    class Auto:
        mode, mt_only = "freqcap", True

        def problem(self):
            return None

        def describe(self):
            return "auto"

    manual = powerlimit.ManualLimiter()
    answers = iter(["3", "1", "2"])  # только однопоток, 20 с, стандартные ступени
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    res = cli.interactive_menu(object(), {"p": [0], "e": [1]}, 10, Auto(), 0, manual)
    assert res["limiter"] is manual


def test_calibrate_does_not_trust_low_max_power():
    # максимум занижен (замер в первую секунду): 25 Вт всё равно надо искать потолком
    cpu = FakeCPU()
    cap, power, status = powerlimit.calibrate(25, cpu.set_cap, cpu.measure, 2000, max_power=24.0)
    assert status == "ok" and cap > 0 and abs(power / 25 - 1) <= 0.05


def test_best_ppw_skips_steps_where_limit_was_not_held():
    def ph(ppw, held):
        return dict(_phase(8, ppw * 10, 10), ppw=ppw, limit_held=held)

    meta = {"date": "d", "cpu": "c", "cores": 4, "threads": 8, "os": "o", "power": "p",
            "duration": 20, "warmup": 8, "warnings": []}
    steps = [{"target": 10, "applied": 10, "phases": [ph(1e8, True)]},
             {"target": 15, "applied": 15, "phases": [ph(2e8, False)]}]
    assert "при лимите 10 Вт\n" in report.build_sweep_report(meta, steps, "src") + "\n"
    steps[0]["phases"][0]["limit_held"] = False
    assert "при лимите 15 Вт (лимит нигде не соблюдался)" in report.build_sweep_report(meta, steps, "src")


def test_sweep_and_interactive_conflict(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ppw-bench", "--interactive", "--sweep", "10"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "нельзя сочетать" in capsys.readouterr().err
