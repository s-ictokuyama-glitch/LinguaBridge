"""Check a completed replay_client report against the 50-minute lesson contract."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def evaluate(run):
    missing = [key for key in ("latency", "drift", "memory", "restore", "final_stats",
                               "mt_cache", "calls", "resource_observation")
               if not isinstance(run.get(key), dict)]
    if missing:
        return dict(status="FAIL", checks={"required_observations": False},
                    failures=["required_observations"], missing=missing,
                    acceptance_reasons=run["verdict"]["reasons"])
    checks = {}
    checks["full_audio"] = (run["minutes"] == 50 and run["measurement_seconds"] >= 3000
                            and run["audio_bytes_sent"] == run["audio_bytes_planned"] == 96_000_000)
    checks["real_inference"] = (run["asr_engine"] in ("haas-onnx", "sherpa", "faster-whisper")
                                and run["engine"] in ("hy-mt2", "nllb")
                                and run["mt_cache_size"] == 0 and run["mt_cache"]["hits"] == 0
                                and run["calls"]["asr"] > 0 and run["calls"]["mt"] > 0)
    checks["classroom"] = (run["students"] == run["measurement_students"] == 10
                           and run["measurement_langs"] == {"en": 5, "zh": 5})
    checks["no_failures"] = (run["errors"] == [] and run["crashes"] == 0
                             and run["disconnects"] == run["reconnect_failures"] == 0
                             and run["disconnect_every_s"] is None
                             and run["dropped"]["segments"] == run["dropped"]["frames_rejected"] == 0
                             and run["dropped"]["seconds"] == 0 and run["dropped"]["notices"] == {})
    published = run["restore"]["published_seqs"]
    deliveries = run["restore"]["continuous_delivery_by_student"]
    checks["every_student"] = (published > 0 and len(deliveries) == 10 and all(
        d["published"] == d["expected"] == d["received"] == published
        and d["gaps"] == 0 and d["missing_seqs"] == [] and d["exempt_own_history"] == []
        for d in deliveries.values()))
    drift = run["drift"]
    checks["whole_lesson_captions"] = (drift["window_s"] == 300 and drift["empty_windows"] == 0
                                       and [w["index"] for w in drift["windows"]] == list(range(10))
                                       and all(w["count"] > 0 for w in drift["windows"]))
    latency = run["latency"]
    checks["latency"] = (latency["count"] == run["captions"] == published * 2
                         and latency["median_s"] <= 5 and latency["max_s"] <= 8
                         and drift["drifting"] is False)
    memory = run["memory"]
    resources = run["resources"]
    checks["resources"] = (memory["peak_mb"] <= 5000 and memory["increasing"] is False
                            and {r["name"] for r in resources} == {
                                "threads", "handles", "tasks", "audio_queue_seconds", "mt_queue_depth"}
                            and all(r["growing"] is False for r in resources))
    observation = run["resource_observation"]
    checks["continuous_observation"] = (
        all(isinstance(observation[f"{kind}_max_gap_s"], (int, float))
            and 0 <= observation[f"{kind}_max_gap_s"] <= 6 for kind in ("process", "stats"))
        and all(observation[f"{kind}_samples"] >= 500
                for kind in ("process", "stats", "rss", "thread", "handle")))
    final = run["final_stats"]
    checks["drained"] = all(final[field] == 0 for field in (
        "queue_depth", "audio_queue_seconds", "mt_queue_depth"))
    checks["acceptance"] = run["verdict"]["passed"] is True
    return dict(status="PASS" if all(checks.values()) else "FAIL", checks=checks,
                failures=[key for key, passed in checks.items() if not passed],
                acceptance_reasons=run["verdict"]["reasons"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    from scripts.performance_evidence import recheck_validation_witness, validate_performance_manifest

    path = args.report.resolve()
    if args.json and args.json.resolve().is_relative_to(path.parent):
        parser.error("--json must be outside the completed run directory")
    validated = validate_performance_manifest(path.parent, kind="soak", primary_report=path, root=ROOT)
    report_bytes = path.read_bytes()
    report = json.loads(report_bytes)
    if len(report["results"]) != 1:
        raise ValueError("one explicit engine run is required")
    run = report["results"][0]
    result = evaluate(run)
    recheck_validation_witness(validated)
    result["source_report"] = str(path)
    result["source_report_sha256"] = hashlib.sha256(report_bytes).hexdigest()
    result["system"] = report["system"]
    result["observed"] = {key: run[key] for key in (
        "measurement_seconds", "audio_bytes_sent", "captions", "latency", "memory",
        "calls", "errors", "dropped", "cpu_percent", "audio_queue_seconds")}
    result["scope"] = "one real-time run on the recorded PC and synthetic corpus; no ASR/MT quality or LAN guarantee"
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return int(result["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
