from __future__ import annotations

import json

import pytest

from scripts.asr_adoption import evaluate_json, main


def measurements(**overrides):
    return {
        "cer": 0.0564,
        "median_delay_ms": 5000,
        "max_delay_ms": 8000,
        "peak_rss_bytes": 4_500_000_000,
        "package_growth_bytes": 1_000_000_000,
        **overrides,
    }


def record(candidates, *, baseline_cer=0.0705):
    return json.dumps({"baseline_cer": baseline_cer, "candidates": candidates})


def test_limits_are_inclusive():
    result = evaluate_json(record({"haas": measurements()}))

    assert result.adopted == "haas"
    assert result.candidates["haas"].passed
    assert result.candidates["haas"].reasons == []


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("median_delay_ms", 5000.1, "遅延中央値"),
        ("max_delay_ms", 8001, "遅延最大"),
        ("peak_rss_bytes", 4_500_000_001, "ピーク RSS"),
        ("package_growth_bytes", 1_000_000_001, "配布容量増"),
        ("cer", 0.05640000000001, "CER"),
    ],
)
def test_just_over_each_limit_is_rejected(field, value, reason):
    result = evaluate_json(record({"haas": measurements(**{field: value})}))

    assert result.adopted is None
    assert not result.candidates["haas"].passed
    assert len(result.candidates["haas"].reasons) == 1
    assert reason in result.candidates["haas"].reasons[0]


def test_selects_best_cer_of_passing_candidates():
    result = evaluate_json(
        record(
            {
                "slower": measurements(cer=0.02, max_delay_ms=8001),
                "kotoba": measurements(cer=0.054),
                "haas": measurements(cer=0.050, package_growth_bytes=-100),
            }
        )
    )

    assert result.adopted == "haas"
    assert result.candidates["kotoba"].passed
    assert not result.candidates["slower"].passed


def test_equal_cer_breaks_ties_by_candidate_name():
    result = evaluate_json(record({"z": measurements(), "a": measurements()}))

    assert result.adopted == "a"
    assert all(verdict.passed for verdict in result.candidates.values())


def test_zero_baseline_cannot_be_improved():
    result = evaluate_json(record({"haas": measurements(cer=0)}, baseline_cer=0))

    assert result.adopted is None
    assert not result.candidates["haas"].passed
    assert "現行 CER が 0" in result.candidates["haas"].reasons[0]


def test_no_candidates_returns_no_adoption():
    result = evaluate_json(record({}))

    assert result.adopted is None
    assert result.candidates == {}


@pytest.mark.parametrize("invalid", [None, True, "0.01", float("nan"), -0.01])
def test_invalid_cer_cannot_pass(invalid):
    with pytest.raises(ValueError, match="haas.cer"):
        evaluate_json(record({"haas": measurements(cer=invalid)}))


def test_cli_reads_measurement_record_and_prints_json(tmp_path, capsys):
    path = tmp_path / "measurements.json"
    path.write_text(record({"haas": measurements()}), encoding="utf-8")

    assert main([str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "adopted": "haas",
        "candidates": {"haas": {"passed": True, "reasons": []}},
    }
