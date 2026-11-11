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
    assert res == {"suite": "cpu", "duration": 60.0, "sweep": None, "limiter": None, "pin": "all",
                   "no_st": False, "only_st": False}


def test_mt_only_120s(monkeypatch):
    res = run_menu(monkeypatch, ["2", "3"])
    assert res["duration"] == 120.0 and res["no_st"] and not res["only_st"]


def test_st_only_20s(monkeypatch):
    res = run_menu(monkeypatch, ["3", "1"])
    assert res["duration"] == 20.0 and res["only_st"] and res["pin"] == "all"


def test_non_hybrid_uses_auto_pin(monkeypatch):
    res = run_menu(monkeypatch, ["1", "1"], tiers=None)
    assert res["pin"] == "auto"


def test_back_and_sensors_then_exit(monkeypatch):
    # 4 = датчики, Enter = вернуться, 1 -> 0 = назад, 0 = выход
    assert run_menu(monkeypatch, ["4", "", "1", "0", "0"]) is None


@pytest.mark.parametrize("suite,tiers,expected", [
    ("cpu", MTL, 4), ("st", MTL, 3), ("mt", MTL, 1),
    ("cpu", None, 2), ("st", {"p": [0], "e": [1]}, 2),
])
def test_phase_count(suite, tiers, expected):
    assert cli.suite_phase_count(suite, tiers) == expected


def test_estimate():
    assert cli.estimate_seconds(4, 60, 10) == 4 * 63 + 30
    assert cli.estimate_seconds(1, 20, 10) == 23
