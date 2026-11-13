"""Check teacher capture histories and replay the actual Worklet PCM into captions."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SYNC_COMMIT = "4efcb9f6edd2b1e39aea9c173d9b8b29870861f1"
CAPTURE = Path(__file__).with_suffix(".cjs")


def run_capture(source_root, directory, seconds, case=None):
    command = ["node", str(CAPTURE), "--source-root", str(source_root),
               "--json", str(directory / "capture.json"), "--pcm", str(directory / "audio.pcm"),
               "--seconds", str(seconds)]
    if case:
        command += ["--case", case]
    child = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=120)
    if child.returncode not in (0, 1) or not (directory / "capture.json").is_file():
        raise RuntimeError(f"capture harness failed: {child.stderr}\n{child.stdout}")
    report = json.loads((directory / "capture.json").read_text(encoding="utf-8"))
    if report["status"] not in ("PASS", "FAIL") or child.returncode != (report["status"] == "FAIL"):
        raise RuntimeError(f"inconsistent capture result: {report}")
    return report


async def replay(path, seconds):
    import numpy as np
    from lesson_endurance import Clock, TimedExecutor, clock_scope
    from lesson_liveness import Scenario, Violation, callbacks
    from server.connection import Connection
    from server.inference import InferenceRunner
    from server.lesson import Lesson

    scenario = Scenario(defaults=True)
    clock = Clock()
    scenario.asr = TimedExecutor(clock, 1)
    scenario.mt = TimedExecutor(clock, 2)
    scenario.pipeline._asr_runner.stop()
    scenario.pipeline.captions._mt_runner.stop()
    scenario.pipeline._asr_runner = InferenceRunner(scenario.asr)
    scenario.pipeline.captions._mt_runner = InferenceRunner(scenario.mt)
    scenario.inputs = list(range(1, seconds // 5 + 1))
    lesson = Lesson(scenario.pipeline, scenario.config, join_code="4831", teacher_code="604831")
    teacher = Connection(scenario.teacher, "127.0.0.1", lesson, scenario.config.limits)
    expected_packets = seconds * 10
    observation = dict(status="PASS", seconds=seconds, expected_utterances=seconds // 5,
                       expected_packets=expected_packets, asr_service_s=0.1, mt_service_s=0.2,
                       pipeline_stop_returned=False)
    with clock_scope(clock):
        try:
            await teacher.receive(json.dumps(dict(type="join", role="teacher", code="604831")))
            for lang, socket in scenario.students.items():
                connection = Connection(socket, "127.0.0.1", lesson, scenario.config.limits)
                await connection.receive(json.dumps(dict(type="join", role="student", code="4831", lang=lang)))
            await scenario.pipeline.start()
            await callbacks()
            if not scenario.pipeline.ready:
                raise RuntimeError("pipeline warmup did not settle")
            await teacher.receive(json.dumps(dict(type="control", action="start")))
            packets = 0
            with path.open("rb") as audio:
                for tick in range(expected_packets + 100):
                    clock.tick = tick
                    scenario.asr.release_due()
                    scenario.mt.release_due()
                    await callbacks()
                    scenario.trace = [f"worklet_pcm_time={min(tick, expected_packets) / 10}s"]
                    if tick < expected_packets:
                        frame = audio.read(3200)
                        scenario.require(len(frame) == 3200, "pcm_framing", 3200, len(frame))
                        identity = tick // 50 + 1
                        expected_sample = 1000 + identity if tick % 50 < 40 else 0
                        samples = np.frombuffer(frame, dtype="<i2")
                        scenario.require(bool(np.all(samples == expected_sample)), "pcm_samples",
                                         expected_sample, np.unique(samples).tolist()[:8])
                        await teacher.receive(frame)
                        packets += 1
                        await callbacks()
                    scenario.workers_alive()
                    scenario.require(scenario.pipeline.asr_dropped_segments == 0,
                                     "caption_delivery", "no input dropped",
                                     scenario.pipeline.asr_dropped_segments)
                scenario.require(not audio.read(1), "pcm_framing", "end of stream", "extra bytes")
            scenario.compare()
            scenario.require(not any(scenario.outstanding().values()), "caption_delivery",
                             "no pending inference after 10 virtual seconds", scenario.outstanding())
            observation.update(packets=packets, asr_calls=scenario.pipeline.asr_calls,
                               mt_calls=scenario.pipeline.captions.metrics().calls)
        except Violation as error:
            observation.update(status="FAIL", violation=error.detail)
        finally:
            observation["captions"] = {
                lang: sum(frame["type"] == "caption" for frame in socket.frames)
                for lang, socket in scenario.students.items()
            }
            await scenario.close()
            observation["pipeline_stop_returned"] = True
    return observation


def check(source_root, seconds, case=None):
    with tempfile.TemporaryDirectory(prefix="teacher-audio-") as temporary:
        directory = Path(temporary)
        capture = run_capture(source_root, directory, seconds, case)
        bridge = asyncio.run(replay(directory / "audio.pcm", seconds))
    return dict(status="PASS" if capture["status"] == bridge["status"] == "PASS" else "FAIL",
                sync_commit=SYNC_COMMIT,
                tools=dict(python=platform.python_version(),
                           node=subprocess.check_output(["node", "--version"], text=True).strip()),
                capture=capture, translation=bridge)


MUTATIONS = (
    ("initial_context_never_resumed", "initial", "capture_liveness", "web/teacher.js",
     "await ctx.resume();", "// injected: leave the initial context suspended"),
    ("suspended_context_never_resumed", "resume_suspended", "capture_liveness", "web/teacher.js",
     "await state.audioCtx.resume();", "void 0;"),
    ("ended_track_never_reacquired", "resume_ended", "capture_liveness", "web/teacher.js",
     'request = requestMic("recover", storageGet(STORAGE_MIC_DEVICE) || "");', 'request = null;'),
    ("audio_sent_while_paused", "paused", "inactive_quiet", "web/teacher.js",
     'state.sessionState === "live" &&', 'true &&'),
    ("wrong_pcm_packet_size", "initial", "pcm_framing", "web/audio-worklet.js",
     "const CHUNK_SAMPLES = 1600;", "const CHUNK_SAMPLES = 1601;"),
    ("wrong_pcm_samples", "initial", "pcm_samples", "web/audio-worklet.js",
     "clamped * 0x7fff", "clamped * 0x3fff"),
    ("wrong_translation", "initial", "caption_delivery", "server/caption_delivery.py",
     "text=translated.text,", 'text="wrong translation",'),
)


def violations(report):
    result = list(report["capture"]["bounded"]["violations"])
    if report["translation"].get("violation"):
        result.append(report["translation"]["violation"])
    return result


def child_check(source_root, output, case=None):
    output.unlink(missing_ok=True)
    command = [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
               "--source-root", str(source_root), "--seconds", "5", "--json", str(output)]
    if case:
        command += ["--case", case]
    child = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=60)
    if child.returncode not in (0, 1) or not output.is_file():
        raise RuntimeError(f"mutation child failed: {child.stderr}\n{child.stdout}")
    result = json.loads(output.read_text(encoding="utf-8"))
    if child.returncode != (result["status"] == "FAIL"):
        raise RuntimeError("mutation child status and exit code disagree")
    return result


def mutation_check(source_root, baseline):
    results = []
    with tempfile.TemporaryDirectory(prefix="teacher-audio-mutations-") as temporary:
        directory = Path(temporary)
        for name, case, property_name, relative, before, after in MUTATIONS:
            healthy = child_check(source_root, directory / "healthy.json", case)
            if healthy["status"] != "PASS":
                raise RuntimeError(f"mutation control is not healthy: {name}")
            scratch = directory / name
            for part in ("web", "server"):
                shutil.copytree(source_root / part, scratch / part,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = scratch / relative
            source = target.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"mutation site changed: {name}")
            target.write_text(source.replace(before, after), encoding="utf-8")
            broken = child_check(scratch, directory / "broken.json", case)
            violation = next((item for item in violations(broken) if item["property"] == property_name), None)
            if broken["status"] != "FAIL" or violation is None:
                raise RuntimeError(f"mutation missed {property_name}: {name}")
            results.append(dict(name=name, healthy="PASS", detected=True, violation=violation))
        restored = child_check(source_root, directory / "restored.json")
    if restored["capture"]["bounded"] != baseline["capture"]["bounded"] or restored["translation"]["status"] != "PASS":
        raise RuntimeError("restored source does not reproduce the baseline")
    return dict(results=results, restored=restored["status"],
                restored_bounded_matches=True, restored_translation="PASS")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--seconds", type=int, default=3000)
    parser.add_argument("--case", choices=("initial", "paused", "resume_suspended", "resume_ended"))
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if not 5 <= args.seconds <= 3000 or args.seconds % 5:
        parser.error("seconds must be a multiple of 5 between 5 and 3000")
    if args.case and args.mutation_check:
        parser.error("mutation-check requires the complete bounded exploration")
    source_root = args.source_root.resolve()
    sys.path.insert(0, str(source_root))
    logging.disable(logging.CRITICAL)
    try:
        report = check(source_root, args.seconds, args.case)
        if args.mutation_check:
            report["mutations"] = mutation_check(source_root, report)
        report["source_sha256"] = {
            path: hashlib.sha256((source_root / path).read_bytes()).hexdigest()
            for path in ("web/teacher.js", "web/audio-worklet.js", "server/connection.py",
                         "server/lesson.py", "server/pipeline.py", "server/caption_delivery.py")
        }
        code = int(report["status"] != "PASS")
    except Exception as error:
        report = dict(status="ERROR", error=f"{type(error).__name__}: {error}")
        code = 2
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return code


if __name__ == "__main__":
    sys.exit(main())
