# -*- coding: utf-8 -*-
"""Единицы в отчёте для профилей нагрузки (AVX2 — FLOPS, AES — байты)."""

from ppwbench import report

AVX2 = {"threads": 22, "profile": "avx2", "unit": ("FLOPS", 192.0)}
AES = {"threads": 22, "profile": "aes", "unit": ("B/s", 128.0)}
SCALAR = {"threads": 22, "profile": "scalar", "unit": None}


def test_rates_in_profile_units():
    assert report.fmt_rate(AVX2, 3e9) == "576.000 GFLOPS"
    assert report.fmt_ppw(AVX2, 1e8) == "19.200 GFLOPS на Вт"
    assert report.fmt_rate(AES, 5e8) == "64.000 GB/s"
    assert report.fmt_total(AES, 1e9) == "128.000 GB"
    assert report.fmt_rate(SCALAR, 3.3e9) == "3.300 Gед/с"
    assert report.fmt_ppw(SCALAR, 1.1e8, short=True) == "110.000 Mед/с/Вт"


def test_phase_names():
    assert report.phase_name(AVX2) == "MT (AVX2+FMA)"
    assert report.phase_name(SCALAR) == "MT"
    assert report.phase_name({"threads": 1, "profile": "scalar", "core_kind": "P"}) == "ST (P)"


def test_sweep_csv_has_scaled_columns():
    ph = dict(AVX2, work_per_s=3e9, power_w=20.0, pkg_w=20.0, ppw=1.5e8, freq={"mhz": 2000.0})
    rows = report.sweep_rows([{"target": 20, "applied": 20, "phases": [ph]}])
    assert rows[0]["profile"] == "avx2" and rows[0]["rate_unit"] == "FLOPS"
    assert rows[0]["rate"] == "576000000000" and rows[0]["ppw_rate"] == "28800000000"
