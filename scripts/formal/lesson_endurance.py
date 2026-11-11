"""Bounded positive deadlines, uninterrupted lesson input, and stuck native workers."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
import itertools
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SYNC_COMMIT = "1f5bc911317534a3c6b93cf0941d27a0189c8745"
sys.path.insert(0, str(Path(__file__).parent))
from lesson_liveness import ControlledExecutor, Recognition, Scenario, Translation, Violation, callbacks, japanese


@dataclass
class Clock:
    tick: int = 0

    def __call__(self):
        return 10000 + self.tick / 10


class TimedExecutor(ControlledExecutor):
    def __init__(self, clock, service_ticks):
        super().__init__()
        self.clock = clock
        self.service_ticks = service_ticks
        self.due = None
        self.started = 0

    def _start(self):
        while self.jobs and self.jobs[0].future.cancelled():
            self.jobs.popleft()
        if self.jobs and not self.jobs[0].future.running():
            self.jobs[0].future.set_running_or_notify_cancel()
            self.started += 1
            self.due = self.clock.tick + self.service_ticks

    def release_due(self):
        if self.jobs and self.due is not None and self.due <= self.clock.tick:
            self.due = None
            job = self.jobs.popleft()
            try:
                job.future.set_result(job.operation())
            except Exception as error:
                job.future.set_exception(error)
            self._start()


def clock_scope(clock):
    stack = ExitStack()
    stack.enter_context(patch.object(asyncio.get_running_loop(), "time", clock))
    stack.enter_context(patch("time.monotonic", clock))
    stack.enter_context(patch("server.inference.monotonic", clock))
    return stack


async def lesson(seconds, voice_ticks, asr_ticks, mt_ticks):
    import numpy as np
    from server.inference import InferenceRunner

    clock = Clock()
    scenario = Scenario(defaults=True)
    scenario.asr = TimedExecutor(clock, asr_ticks)
    scenario.mt = TimedExecutor(clock, mt_ticks)
    scenario.pipeline._asr_runner.stop()
    scenario.pipeline.captions._mt_runner.stop()
    scenario.pipeline._asr_runner = InferenceRunner(scenario.asr)
    scenario.pipeline.captions._mt_runner = InferenceRunner(scenario.mt)
    period = voice_ticks + 10
    total_ticks = seconds * 10
    assert total_ticks % period == 0
    peaks = dict(audio_seconds=0, mt_jobs=0, history=0, cache=0, receipts=0, tasks=0)
    first_caption_at = {}
    observation = dict(status="PASS", seconds=seconds, voice_seconds=voice_ticks / 10,
                       asr_service_seconds=asr_ticks / 10, mt_service_seconds=mt_ticks / 10)
    with clock_scope(clock):
        try:
            await scenario.start()
            task_bound = len(asyncio.all_tasks())
            silence = bytes(3200)
            voice = silence
            for tick in range(total_ticks + 100):
                clock.tick = tick
                scenario.asr.release_due()
                scenario.mt.release_due()
                await callbacks()
                if tick < total_ticks:
                    identity = tick // period + 1
                    if tick % period == 0:
                        scenario.inputs.append(identity)
                        voice = np.full(1600, 1000 + identity, dtype="<i2").tobytes()
                    await scenario.pipeline.feed_audio(voice if tick % period < voice_ticks else silence)
                    await callbacks()
                scenario.trace = [f"pcm_time={min(tick, total_ticks) / 10}s",
                                  f"asr={asr_ticks / 10}s mt={mt_ticks / 10}s"]
                scenario.workers_alive()
                p = scenario.pipeline
                sizes = dict(audio_seconds=p.audio_queue_seconds, mt_jobs=p.captions.queue_depth,
                             history=len(p.captions.history.since(0)),
                             cache=p.captions.metrics().cache.size,
                             receipts=max(len(c.caption_languages) for c in scenario.roster.students()),
                             tasks=len(asyncio.all_tasks()))
                for key, value in sizes.items():
                    peaks[key] = max(peaks[key], value)
                bounds = dict(audio_seconds=scenario.config.limits.asr_queue_seconds,
                              mt_jobs=scenario.config.limits.mt_queue_max,
                              history=scenario.config.history_resend,
                              cache=scenario.config.mt.cache_size,
                              receipts=2 * scenario.config.history_resend, tasks=task_bound + 4)
                scenario.require(all(sizes[k] <= bounds[k] for k in sizes),
                                 "retained_state_bound", bounds, sizes)
                scenario.require(p.asr_dropped_segments == 0, "uninterrupted_admission", 0,
                                 p.asr_dropped_segments)
                for lang, socket in scenario.students.items():
                    for frame in socket.frames[-4:]:
                        if frame["type"] == "caption":
                            first_caption_at.setdefault((lang, frame["seq"]), tick / 10)
                scenario.require(all(s.closed is None for s in scenario.students.values()),
                                 "healthy_connection", "open", [s.closed for s in scenario.students.values()])
                due_count = max(0, (tick - 80 - voice_ticks) // period + 1)
                for lang, socket in scenario.students.items():
                    delivered = sum(f["type"] == "caption" for f in socket.frames)
                    scenario.require(delivered >= min(due_count, len(scenario.inputs)),
                                     "caption_deadline", dict(lang=lang, at_least=due_count), delivered)
            scenario.compare()
            scenario.require(not any(scenario.outstanding().values()),
                             "final_backlog", 0, scenario.outstanding())
            errors = [f for f in scenario.teacher.frames if f["type"] == "error"]
            scenario.require(not errors, "healthy_errors", [], errors[:3])
            delays = [at - ((seq - 1) * period + voice_ticks) / 10
                      for (_, seq), at in first_caption_at.items()]
            observation.update(inputs=len(scenario.inputs), captions_per_language=len(scenario.inputs),
                               max_caption_after_speech_s=round(max(delays), 3), peaks=peaks)
        finally:
            await scenario.close()
    return observation


async def deadline_case(service_ticks, release_before_timer):
    from server.inference import InferenceRunner, InferenceSucceeded, InferenceTimedOut

    clock = Clock()
    executor = TimedExecutor(clock, service_ticks)
    runner = InferenceRunner(executor)
    with clock_scope(clock):
        try:
            first = asyncio.create_task(runner.run(lambda: "first", timeout=1.0))
            await callbacks()
            for tick in range(1, 13):
                clock.tick = tick
                if release_before_timer:
                    executor.release_due()
                await callbacks()
                if not release_before_timer:
                    executor.release_due()
                    await callbacks()
            if not first.done():
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
                raise Violation("positive_deadline", "caller finishes by 1.2 seconds", "pending", [])
            result = await first
            valid = (isinstance(result, InferenceSucceeded) if service_ticks < 10 else
                     isinstance(result, InferenceTimedOut) if service_ticks > 10 else
                     isinstance(result, (InferenceSucceeded, InferenceTimedOut)))
            if not valid:
                raise Violation("positive_deadline", "success before deadline; timeout after deadline",
                                type(result).__name__, [f"service_ticks={service_ticks}",
                                                        f"release_before_timer={release_before_timer}"])
            executor.service_ticks = 1
            next_call = asyncio.create_task(runner.run(lambda: "next", timeout=1.0))
            await callbacks()
            clock.tick += 1
            executor.release_due()
            await callbacks()
            if not next_call.done():
                next_call.cancel()
                await asyncio.gather(next_call, return_exceptions=True)
                raise Violation("deadline_continuation", "next call completes", "still pending", [])
            outcome = await next_call
            if not isinstance(outcome, InferenceSucceeded) or outcome.value != "next":
                raise Violation("deadline_continuation", "next", repr(outcome), [])
            return dict(service_s=service_ticks / 10, release_before_timer=release_before_timer,
                        first=type(result).__name__, next=type(outcome).__name__)
        finally:
            runner.stop()


class HungRecognition(Recognition):
    def __init__(self, directory, hang=True):
        self.directory = Path(directory)
        self.hang = hang

    def warmup(self):
        (self.directory / f"loaded-asr-{os.getpid()}").touch()

    def transcribe(self, pcm, sample_rate):
        identity = int(pcm[pcm != 0][0]) - 1000
        if self.hang and identity in (1, 3):
            block_operation(self.directory, "asr", identity)
        return super().transcribe(pcm, sample_rate)


class HungTranslation(Translation):
    def __init__(self, directory, hang=True):
        self.directory = Path(directory)
        self.hang = hang

    def warmup(self):
        (self.directory / f"loaded-mt-{os.getpid()}").touch()

    def translate(self, text, lang):
        if self.hang and lang == "en":
            for identity in (1, 3):
                if text == japanese(identity):
                    block_operation(self.directory, "mt", identity)
        return super().translate(text, lang)


def block_operation(directory, kind, identity):
    (directory / f"entered-{kind}-{identity}").write_text(str(os.getpid()), encoding="ascii")
    while not (directory / f"release-{kind}-{identity}").exists():
        time.sleep(0.01)


def pid_exists(pid):
    if sys.platform == "win32":
        import _winapi
        try:
            handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, pid)
        except OSError as error:
            if error.winerror == 87:  # PID does not exist.
                return False
            raise
        try:
            return _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT
        finally:
            _winapi.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def observe_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


async def native_hang(kind, destination, release_hang=False):
    from server.inference_process import ProcessInferenceRunner
    from server.pipeline import Pipeline

    scenario = Scenario(defaults=True)
    scenario.config.limits.asr_timeout_s = 2
    scenario.config.limits.mt_timeout_s = 2
    directory = destination.parent
    make_asr = partial(HungRecognition, directory, hang=kind == "asr")
    make_mt = partial(HungTranslation, directory, hang=kind == "mt")
    asr, mt = make_asr(), make_mt()
    scenario.pipeline = Pipeline(
        scenario.roster, scenario.config, asr, mt,
        asr_runner=ProcessInferenceRunner(asr, make_asr,
                                        startup_timeout_s=scenario.config.limits.inference_startup_timeout_s,
                                        name="endurance-asr"),
        mt_runner=ProcessInferenceRunner(mt, make_mt,
                                       startup_timeout_s=scenario.config.limits.inference_startup_timeout_s,
                                       name="endurance-mt"),
    )
    report = dict(kind=kind, released_control=release_hang, status="FAIL",
                  property="native_hang_recovery", timeout_s=2, recovery_observation_s=8,
                  expected="second utterance reaches both languages and every child is reaped",
                  pipeline_stop_returned=False,
                  scope="real process backend; finite test watchdogs, not a recovery latency SLO")
    try:
        await scenario.roster.add(scenario.teacher, "teacher")
        for lang, socket in scenario.students.items():
            await scenario.roster.add(socket, "student", lang)
        startup_started = time.monotonic()
        await scenario.pipeline.start()
        await asyncio.wait_for(scenario.pipeline._warmup_task, timeout=10)
        if not scenario.pipeline.ready:
            raise RuntimeError("native witness warmup failed")
        report["initial_startup_s"] = round(time.monotonic() - startup_started, 3)
        await scenario.pipeline.set_accepting_audio(True)
        await scenario.feed(1)
        entered = directory / f"entered-{kind}-1"
        if not await observe_until(entered.exists, 5):
            raise RuntimeError("native operation did not enter the injected hang")
        old_pid = int(entered.read_text(encoding="ascii"))
        if release_hang:
            (directory / f"release-{kind}-1").touch()
        else:
            await observe_until(lambda: any(f.get("code") == f"{kind}_timeout"
                                            for f in scenario.teacher.frames), 5)
        report["old_pid"] = old_pid
        report["old_pid_reaped_after_timeout"] = None if release_hang else not pid_exists(old_pid)
        recovery_started = time.monotonic()
        await scenario.feed(2)
        expected_seq = 1 if kind == "asr" and not release_hang else 2
        expected = {lang: [dict(seq=expected_seq, ja=japanese(2), lang=lang, text=f"{lang}:{japanese(2)}")]
                    for lang in scenario.students}
        def captions():
            return {lang: [{key: frame[key] for key in ("seq", "ja", "lang", "text")}
                           for frame in socket.frames if frame["type"] == "caption" and frame["ja"] == japanese(2)]
                    for lang, socket in scenario.students.items()}
        def reached():
            return {lang: frames == expected[lang] for lang, frames in captions().items()}
        await observe_until(lambda: all(reached().values()), 8)
        report.update(actual=reached(), recovery_elapsed_s=round(time.monotonic() - recovery_started, 3),
                      expected_captions=expected, actual_captions=captions(),
                      injected_operation_never_released=not release_hang,
                      trace=[f"{kind} enters input-specific blocking operation",
                             "release control" if release_hang else "deadline expires and old process is reaped",
                             "feed second utterance", "observe both language captions",
                             "feed third utterance and stop while blocked"],
                      notices=[f["code"] for f in scenario.teacher.frames if f["type"] == "error"])
        await scenario.feed(3)
        stop_marker = directory / f"entered-{kind}-3"
        report["stop_while_blocked"] = await observe_until(stop_marker.exists, 5)
        report["stop_hang_pid"] = int(stop_marker.read_text()) if stop_marker.exists() else None
        report["stop_hang_pid_alive"] = (report["stop_hang_pid"] is not None
                                         and pid_exists(report["stop_hang_pid"]))
        destination.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    finally:
        await scenario.close()
    report["pipeline_stop_returned"] = True
    pids = sorted(int(path.name.rsplit("-", 1)[1]) for path in directory.glob("loaded-*-*"))
    report["child_pids"] = pids
    report["all_children_reaped"] = all(not pid_exists(pid) for pid in pids)
    report["status"] = "PASS" if (
        all(report["actual"].values()) and report["all_children_reaped"]
        and report["stop_while_blocked"] and report["stop_hang_pid_alive"]
        and (release_hang or (report["old_pid_reaped_after_timeout"] and f"{kind}_timeout" in report["notices"]))
    ) else "FAIL"
    destination.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


def hang_witness(kind, source_root, release_hang=False):
    with tempfile.TemporaryDirectory(prefix="lesson-hang-") as directory:
        result_path = Path(directory) / "result.json"
        command = [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                   "--source-root", str(source_root), "--native-child", kind, "--json", str(result_path)]
        if release_hang:
            command.append("--release-hang")
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8")
        try:
            deadline = time.monotonic() + 35
            while not result_path.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            try:
                stdout, stderr = child.communicate(timeout=2)
                exited = True
            except subprocess.TimeoutExpired:
                child.kill()
                stdout, stderr = child.communicate(timeout=5)
                exited = False
            if not result_path.exists():
                raise RuntimeError(f"native witness did not publish a result: {stdout}\n{stderr}")
            if exited and child.returncode:
                raise RuntimeError(f"native witness crashed: {stderr}")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["process_exit"] = dict(status="PASS" if exited else "FAIL", after_result_watchdog_s=2,
                                          expected="interpreter exits after Pipeline.stop",
                                          actual="exited" if exited else "blocked; witness child killed")
            if release_hang and (result["status"] != "PASS" or not exited):
                raise RuntimeError(f"released native control failed: {result}")
            return result
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


async def controlled_checks(quick=False):
    report = dict(deadlines=[], lessons=[], violations=[])
    for ticks, order in itertools.product((9, 10, 11), (False, True)):
        try:
            report["deadlines"].append(await deadline_case(ticks, order))
        except Violation as error:
            report["violations"].append(error.detail)
    profiles = [(40, 4, 15)] if quick else itertools.product((40, 290), (1, 4), (2, 15))
    for voice, asr, mt in profiles:
        try:
            report["lessons"].append(await lesson(3000, voice, asr, mt))
        except Violation as error:
            report["violations"].append(error.detail)
    report["status"] = "FAIL" if report["violations"] else "PASS"
    return report


MUTATIONS = (
    ("double_deadline", "positive_deadline", "server/inference.py",
     "asyncio.wait_for(asyncio.wrap_future(future), timeout)",
     "asyncio.wait_for(asyncio.wrap_future(future), None if timeout is None else timeout * 2)"),
    ("stop_asr_after_300", "worker_liveness", "server/pipeline.py",
     "await self._process_segment(event)",
     "await self._process_segment(event)\n                        if self.asr_calls >= 300:\n                            return"),
    ("lose_mt_wakeup", "caption_deadline", "server/caption_delivery.py",
     "        self._mt_work.set()\n        return True", "        return True"),
    ("unbounded_translation_cache", "retained_state_bound", "server/mt/cache.py",
     "            while len(self._entries) > self._maxsize:", "            while False:"),
    ("wrong_caption", "caption_delivery", "server/caption_delivery.py",
     "text=translated.text,", 'text="wrong translation",'),
)


def mutations(source_root):
    reports = []
    for name, property_name, relative, before, after in MUTATIONS:
        with tempfile.TemporaryDirectory(prefix="lesson-endurance-") as directory:
            scratch = Path(directory)
            shutil.copytree(source_root / "server", scratch / "server",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = scratch / relative
            original = target.read_text(encoding="utf-8")
            if original.count(before) != 1:
                raise RuntimeError(f"mutation site changed: {name}")
            target.write_text(original.replace(before, after), encoding="utf-8")
            child = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                                    "--source-root", str(scratch), "--controlled-only", "--quick"],
                                   capture_output=True, encoding="utf-8", timeout=120)
            if child.returncode != 1:
                raise RuntimeError(f"mutation did not fail: {name}\n{child.stdout}\n{child.stderr}")
            broken = json.loads(child.stdout)
            violation = next((v for v in broken["controlled"]["violations"]
                              if v["property"] == property_name), None)
            if violation is None:
                raise RuntimeError(f"mutation failed for another reason: {name}: {broken}")
            target.write_text(original, encoding="utf-8")
            restored = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                                      "--source-root", str(scratch), "--controlled-only", "--quick"],
                                     capture_output=True, encoding="utf-8", timeout=120)
            if restored.returncode or json.loads(restored.stdout)["status"] != "PASS":
                raise RuntimeError(f"restored source did not pass: {name}: {restored.stderr}")
            reports.append(dict(name=name, detected=True, restored="PASS", violation=violation))
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--controlled-only", action="store_true")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--native-child", choices=("asr", "mt"))
    parser.add_argument("--release-hang", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root.resolve()))
    logging.disable(logging.CRITICAL)
    if args.native_child:
        if args.json is None:
            parser.error("--native-child requires --json")
        asyncio.run(native_hang(args.native_child, args.json, args.release_hang))
        return 0
    report = dict(sync_commit=SYNC_COMMIT, python=platform.python_version(),
                  coverage=dict(deadline_cases=6, lesson_profiles=1 if args.quick else 8,
                                seconds_per_profile=3000, native_cases=0 if args.controlled_only else 4),
                  controlled=asyncio.run(controlled_checks(args.quick)),
                  scope="finite timing grid and fixed 3000-second virtual PCM schedules; not CPU performance proof")
    report["native"] = [] if args.controlled_only else [
        hang_witness(k, args.source_root, released) for k, released in itertools.product(("asr", "mt"), (True, False))]
    if args.mutation_check:
        if report["controlled"]["status"] != "PASS":
            raise RuntimeError("healthy controlled baseline must pass before mutations")
        report["mutations"] = mutations(args.source_root)
    report["status"] = "FAIL" if report["controlled"]["status"] != "PASS" or any(
        r["status"] != "PASS" or r["process_exit"]["status"] != "PASS" for r in report["native"]) else "PASS"
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return int(report["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
