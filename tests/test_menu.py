# -*- coding: utf-8 -*-
"""Интерактивное меню: выбор набора тестов и длительности."""

import pytest

from ppwbench import cli

MTL = {"p": [0, 1], "e": [2, 3], "lpe": [4, 5]}


class FakeReader:
    def describe(self):
        return "fake"

    def sensor_lines(self):
        return []

    def hints(self):
        return []


def run_menu(monkeypatch, answers, tiers=MTL):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(it))
    return cli.interactive_menu(FakeReader(), tiers, cooldown=10)


def test_defaults_whole_cpu_60s(monkeypatch):
    res = run_menu(monkeypatch, ["", ""])
    assert res == {"suite": "cpu", "duration": 60.0, "sweep": None, "limiter": None,
                   "isa": ["scalar"], "pin": "all", "mt_pin": "none",
                   "no_st": False, "only_st": False}


def test_mt_only_120s(monkeypatch):
    res = run_menu(monkeypatch, ["2", "3"])
    assert res["duration"] == 120.0 and res["no_st"] and not res["only_st"]


def test_st_only_20s(monkeypatch):
    res = run_menu(monkeypatch, ["4", "1"])
    assert res["duration"] == 20.0 and res["only_st"] and res["pin"] == "all"


def test_non_hybrid_uses_auto_pin(monkeypatch):
    res = run_menu(monkeypatch, ["1", "1"], tiers=None)
    assert res["pin"] == "auto"


def test_back_and_sensors_then_exit(monkeypatch):
    # 5 = датчики, Enter = вернуться, 1 -> 0 = назад, 0 = выход
    assert run_menu(monkeypatch, ["5", "", "1", "0", "0"]) is None


@pytest.mark.parametrize("suite,tiers,expected", [
    ("cpu", MTL, 4), ("st", MTL, 3), ("mt", MTL, 1),
    ("cpu", None, 2), ("st", {"p": [0], "e": [1]}, 2),
])
def test_phase_count(suite, tiers, expected):
    assert cli.suite_phase_count(suite, tiers) == expected


def test_estimate():
    assert cli.estimate_seconds(4, 60, 10) == 4 * 63 + 30
    assert cli.estimate_seconds(1, 20, 10) == 23


ALL = ["scalar", "avx2", "aes"]


def menu(monkeypatch, answers, tiers=MTL, profiles=ALL):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _p="": next(it))
    return cli.interactive_menu(FakeReader(), tiers, cooldown=10, profiles=profiles)


def test_isa_step_presets(monkeypatch):
    # весь CPU -> все наборы -> 20 с
    res = menu(monkeypatch, ["1", "2", "1"])
    assert res["isa"] == ALL and not res["no_st"] and res["pin"] == "all"
    # только однопоток -> только AES -> 60 с
    res = menu(monkeypatch, ["4", "4", "2"])
    assert res["isa"] == ["aes"] and res["only_st"]
    # Enter на шаге наборов -> только обычный
    assert menu(monkeypatch, ["2", "", "1"])["isa"] == ["scalar"]


def test_isa_step_back_and_hidden_presets(monkeypatch, capsys):
    # 0 на шаге наборов возвращает к шагу 1, затем выход
    assert menu(monkeypatch, ["1", "0", "0"]) is None
    capsys.readouterr()
    # CPU без AVX2: варианта «Только AVX2+FMA» нет, «все наборы» = обычный + AES
    res = menu(monkeypatch, ["2", "2", "1"], profiles=["scalar", "aes"])
    assert res["isa"] == ["scalar", "aes"]
    assert "Только AVX2" not in capsys.readouterr().out


def test_isa_step_skipped_without_profiles(monkeypatch, capsys):
    res = menu(monkeypatch, ["2", "1"], profiles=["scalar"])
    assert res["isa"] == ["scalar"] and "наборы инструкций" not in capsys.readouterr().out


def test_mt_per_core_type(monkeypatch):
    res = menu(monkeypatch, ["3", "1", "1"])
    assert res["suite"] == "mttiers" and res["mt_pin"] == "all" and res["no_st"]


def test_mt_per_core_type_hidden_on_flat_cpu(monkeypatch, capsys):
    # на обычном CPU пункта 3 нет: ввод 3 не принимается, дальше 2 (многопоток)
    res = menu(monkeypatch, ["3", "2", "1", "1"], tiers=None)
    assert res["suite"] == "mt"
    assert "Многопоток по ядрам" not in capsys.readouterr().out


@pytest.mark.parametrize("suite,profiles,expected", [
    ("mttiers", 1, 3), ("mttiers", 3, 9), ("cpu", 3, 12), ("st", 2, 6),
])
def test_phase_count_with_isa(suite, profiles, expected):
    assert cli.suite_phase_count(suite, MTL, profiles) == expected


class TiersBackend:
    def core_types(self):
        return MTL


def test_resolve_mt():
    phases, warns = cli.resolve_mt("all", TiersBackend())
    assert [p["tag"] for p in phases] == ["MT-P", "MT-E", "MT-LP-E"] and not warns
    assert phases[0]["cpus"] == [0, 1] and phases[0]["core_kind"] == "P"
    plain, _ = cli.resolve_mt("none", TiersBackend())
    assert plain == [{"tag": "MT", "cpus": None, "label": None, "core_kind": None}]


def test_mt_pin_arg():
    assert cli.mt_pin_arg("ALL") == "all"
    with pytest.raises(Exception):
        cli.mt_pin_arg("auto")


def test_resolve_isa():
    sup = ["scalar", "avx2", "aes"]
    assert cli.resolve_isa(["all"], sup) == (["scalar", "avx2", "aes"], [])
    assert cli.resolve_isa(["aes", "scalar"], sup) == (["scalar", "aes"], [])
    profiles, warns = cli.resolve_isa(["avx2"], ["scalar"])
    assert profiles == [] and "не поддерживается" in warns[0]
    assert cli.resolve_isa(["all"], ["scalar"]) == (["scalar"], [])


def test_isa_arg():
    assert cli.isa_arg("AVX2, aes") == ["avx2", "aes"]
    with pytest.raises(Exception):
        cli.isa_arg("avx512")
