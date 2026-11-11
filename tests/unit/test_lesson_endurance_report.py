from copy import deepcopy

import pytest

from scripts.formal.lesson_endurance_report import evaluate


@pytest.fixture
def complete_run():
    return {
        "minutes": 50, "measurement_seconds": 3000.01,
        "audio_bytes_sent": 96_000_000, "audio_bytes_planned": 96_000_000,
        "asr_engine": "haas-onnx", "engine": "hy-mt2", "mt_cache_size": 0,
        "mt_cache": {"hits": 0}, "calls": {"asr": 600, "mt": 1200},
        "students": 10, "measurement_students": 10, "measurement_langs": {"en": 5, "zh": 5},
        "errors": [], "crashes": 0, "disconnects": 0, "reconnect_failures": 0,
        "disconnect_every_s": None,
        "dropped": {"segments": 0, "frames_rejected": 0, "seconds": 0, "notices": {}},
        "restore": {"published_seqs": 600, "continuous_delivery_by_student": {
            f"s{i}": {"published": 600, "expected": 600, "received": 600,
                      "gaps": 0, "missing_seqs": [], "exempt_own_history": []} for i in range(10)}},
        "drift": {"window_s": 300, "empty_windows": 0, "drifting": False,
                  "windows": [{"index": i, "count": 120} for i in range(10)]},
        "latency": {"count": 1200, "median_s": 2, "max_s": 7}, "captions": 1200,
        "memory": {"peak_mb": 4000, "increasing": False},
        "resources": [{"name": name, "growing": False} for name in (
            "threads", "handles", "tasks", "audio_queue_seconds", "mt_queue_depth")],
        "resource_observation": {"process_max_gap_s": 2.1, "stats_max_gap_s": 2.1,
                                 **{f"{name}_samples": 1500 for name in (
                                     "process", "stats", "rss", "thread", "handle")}},
        "final_stats": {name: 0 for name in (
            "queue_depth", "audio_queue_seconds", "mt_queue_depth")},
        "verdict": {"passed": True, "reasons": []},
    }


def test_complete_observation_passes(complete_run):
    assert evaluate(complete_run)["status"] == "PASS"


@pytest.mark.parametrize(("path", "value", "failure"), [
    (("measurement_seconds",), 2940, "full_audio"),
    (("audio_bytes_sent",), 95_999_999, "full_audio"),
    (("calls", "mt"), 0, "real_inference"),
    (("mt_cache", "hits"), 1199, "real_inference"),
    (("errors",), ["ASR timeout"], "no_failures"),
    (("dropped", "notices"), {"asr_timeout": 1}, "no_failures"),
    (("dropped", "notices"), {"mt_timeout": 1}, "no_failures"),
    (("restore", "continuous_delivery_by_student", "s0", "gaps"), 1, "every_student"),
    (("restore", "continuous_delivery_by_student", "s0", "received"), 599, "every_student"),
    (("drift", "windows"), [{"index": 0, "count": 1200}], "whole_lesson_captions"),
    (("latency", "max_s"), 8.01, "latency"),
    (("resource_observation", "process_max_gap_s"), 60, "continuous_observation"),
    (("resource_observation", "process_max_gap_s"), None, "continuous_observation"),
    (("final_stats", "audio_queue_seconds"), 1, "drained"),
    (("memory", "peak_mb"), 5001, "resources"),
])
def test_incomplete_or_failed_work_cannot_pass(complete_run, path, value, failure):
    run = deepcopy(complete_run)
    target = run
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result = evaluate(run)
    assert result["status"] == "FAIL"
    assert failure in result["failures"]


def test_missing_observation_is_an_error(complete_run):
    del complete_run["drift"]
    assert evaluate(complete_run)["status"] == "FAIL"


@pytest.mark.parametrize("field", ["latency", "drift", "memory", "restore", "final_stats",
                                   "mt_cache", "calls", "resource_observation"])
def test_failed_producer_observation_is_structured_failure(complete_run, field):
    complete_run[field] = None
    result = evaluate(complete_run)
    assert result["status"] == "FAIL"
    assert result["missing"] == [field]
