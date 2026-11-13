"""Bounded startup schedules and classroom recovery witnesses on real processes."""
from __future__ import annotations

import argparse
import asyncio
from functools import partial
import itertools
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
SYNC_COMMIT = "4efcb9f6edd2b1e39aea9c173d9b8b29870861f1"
OPERATIONS = ("deadline", "cancel")
PHASES = ("factory", "warmup")
KINDS = ("asr", "mt")
REQUEST_TIMEOUT = 1.0
STARTUP_TIMEOUT = 5.0


class Violation(Exception):
    def __init__(self, property_name, expected, actual, trace):
        self.detail = dict(property=property_name, expected=expected, actual=actual, trace=list(trace))
        super().__init__(property_name)


def require(condition, property_name, expected, actual, trace):
    if not condition:
        raise Violation(property_name, expected, actual, trace)


def block(directory, marker):
    destination = directory / marker
    pending = destination.with_suffix(".tmp")
    pending.write_text(str(os.getpid()), encoding="ascii")
    pending.replace(destination)
    while not (directory / f"release-{marker}").exists():
        time.sleep(0.01)


def record(directory, method, arguments=()):
    with (directory / f"requests-{os.getpid()}.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(at=time.monotonic(), pid=os.getpid(), method=method,
                                     arguments=arguments), ensure_ascii=False) + "\n")


class Engine:
    model_version = "startup-probe-v1"

    def __init__(self, directory, phase, mode):
        self.directory = Path(directory)
        self.phase = phase
        self.mode = mode
        self.block_startup = False

    def warmup(self):
        record(self.directory, "warmup")
        if self.block_startup and self.phase == "warmup":
            block(self.directory, "startup")

    def transcribe(self, pcm, sample_rate):
        from server.asr.base import ASRResult
        identity = int(pcm[pcm != 0][0]) - 1000
        record(self.directory, "transcribe", [identity])
        self.maybe_hang(identity)
        return ASRResult(f"授業の発話{identity}です。")

    def translate(self, text, lang):
        record(self.directory, "translate", [text, lang])
        if text == "授業の発話1です。" and lang == "en":
            self.maybe_hang(1)
        return f"{lang}:{text}"

    def maybe_hang(self, identity):
        if self.mode == "replacement" and identity == 1:
            (self.directory / "arm-startup").touch()
            block(self.directory, "operation")


def make_engine(directory, phase, mode):
    directory = Path(directory)
    (directory / f"process-{os.getpid()}").touch()
    record(directory, "factory")
    engine = Engine(directory, phase, mode)
    engine.block_startup = mode == "always" or (
        mode == "replacement" and (directory / "arm-startup").exists()
        and not (directory / "startup-consumed").exists()
    )
    if engine.block_startup:
        (directory / "startup-consumed").touch()
        if phase == "factory":
            block(directory, "startup")
    return engine


async def observe(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError("probe did not reach its synchronization marker")
        await asyncio.sleep(0.01)


def process_alive(process):
    try:
        return process.is_alive()
    except ValueError:
        return False


async def emergency_cleanup(runner):
    generation = runner._generation
    if generation is None:
        return
    for task in (generation.startup, generation.request, generation.cleanup):
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*(task for task in (generation.startup, generation.request, generation.cleanup)
                           if task is not None), return_exceptions=True)
    generation.connection.close()
    process = generation.process
    if process_alive(process):
        process.kill()
        await asyncio.to_thread(process.join, 5)
    try:
        process.close()
    except ValueError:
        pass


async def bounded_case(directory, phase, operations, terminal):
    from server.inference import InferenceSucceeded, InferenceTimedOut
    from server.inference_process import ProcessInferenceRunner

    engine = Engine(directory, phase, "always")
    runner = ProcessInferenceRunner(engine, partial(make_engine, directory, phase, "always"),
                                    startup_timeout_s=STARTUP_TIMEOUT, name="startup-enumeration")
    trace = [f"spawn and hold {phase}", "initial request deadline expires"]
    observation = dict(phase=phase, operations=list(operations), terminal=terminal)
    try:
        result = await runner.run(partial(engine.translate, "initial", "en"), timeout=0.03)
        require(isinstance(result, InferenceTimedOut), "startup_waiter_isolation", "InferenceTimedOut",
                type(result).__name__, trace)
        await observe((directory / "startup").exists)
        generation = runner._generation
        require(process_alive(generation.process) and not generation.startup.done(),
                "startup_waiter_isolation", "live generation with startup pending",
                dict(alive=process_alive(generation.process), startup_done=generation.startup.done()), trace)
        for operation in operations:
            trace.append(operation)
            if operation == "deadline":
                result = await runner.run(partial(engine.translate, "waiting", "en"), timeout=0.01)
                require(isinstance(result, InferenceTimedOut), "startup_waiter_isolation", "InferenceTimedOut",
                        type(result).__name__, trace)
            else:
                task = asyncio.create_task(runner.run(partial(engine.translate, "waiting", "en"), timeout=None))
                await observe(runner._gate.locked)
                task.cancel()
                result = (await asyncio.gather(task, return_exceptions=True))[0]
                require(isinstance(result, asyncio.CancelledError), "startup_waiter_isolation",
                        "CancelledError", type(result).__name__, trace)
            require(runner._generation is generation and process_alive(generation.process)
                    and not generation.startup.done(), "startup_waiter_isolation",
                    "same live generation with startup still pending",
                    dict(same=runner._generation is generation, alive=process_alive(generation.process),
                         startup_done=generation.startup.done()), trace)
        if terminal == "release":
            trace.append("release startup and request translation")
            (directory / "release-startup").touch()
            try:
                result = await runner.run(partial(engine.translate, "after-startup", "en"), timeout=5)
            except asyncio.CancelledError:
                raise Violation("startup_resumption", "en:after-startup", "startup task cancelled", trace)
            actual = getattr(result, "value", type(result).__name__)
            require(isinstance(result, InferenceSucceeded) and actual == "en:after-startup",
                    "startup_resumption", "en:after-startup", actual, trace)
        trace.append("close runner while startup is held" if terminal == "close" else "close runner")
        await asyncio.wait_for(runner.aclose(), 5)
        require(not process_alive(generation.process), "startup_shutdown", "child reaped", "child alive", trace)
        observation["status"] = "PASS"
    except Violation as error:
        observation.update(status="FAIL", violation=error.detail)
    finally:
        await emergency_cleanup(runner)
    return observation


async def bounded(directory, single=False):
    cases = []
    schedules = [()] if single else [word for length in range(4)
                                    for word in itertools.product(OPERATIONS, repeat=length)]
    for index, (phase, operations, terminal) in enumerate(itertools.product(PHASES, schedules, ("release", "close"))):
        destination = directory / str(index)
        destination.mkdir()
        cases.append(await bounded_case(destination, phase, operations, terminal))
    return dict(status="FAIL" if any(c["status"] == "FAIL" for c in cases) else "PASS",
                cases=len(cases), violations=[c for c in cases if c["status"] == "FAIL"],
                operations=OPERATIONS, max_length=0 if single else 3, phases=PHASES,
                terminals=["release", "close"])


async def native_case(directory, kind, phase, release):
    from lesson_liveness import Scenario, callbacks
    from lesson_endurance import pid_exists
    from server.inference_process import ProcessInferenceRunner
    from server.pipeline import Pipeline

    scenario = Scenario(defaults=True)
    scenario.config.limits.asr_timeout_s = REQUEST_TIMEOUT
    scenario.config.limits.mt_timeout_s = REQUEST_TIMEOUT
    runners = {}
    engines = {}
    for stage in KINDS:
        folder = directory / stage
        folder.mkdir()
        mode = "replacement" if stage == kind else "healthy"
        engines[stage] = Engine(folder, phase, mode)
        runners[stage] = ProcessInferenceRunner(
            engines[stage], partial(make_engine, folder, phase, mode),
            startup_timeout_s=STARTUP_TIMEOUT, name=f"startup-{stage}")
    scenario.pipeline = Pipeline(scenario.roster, scenario.config, engines["asr"], engines["mt"],
                                 asr_runner=runners["asr"], mt_runner=runners["mt"])
    target = directory / kind
    trace = ["warm up both models", "audio 0 reaches English and Chinese students",
             f"hold {kind} operation for audio 1", "expire operation deadline",
             f"next request starts replacement; its {phase} blocks", "feed audio 2"]
    result = dict(kind=kind, phase=phase, release_startup=release, timeout_s=REQUEST_TIMEOUT,
                  startup_timeout_s=STARTUP_TIMEOUT,
                  property="startup_recovery", trace=trace)
    failure = None
    try:
        await scenario.roster.add(scenario.teacher, "teacher")
        for lang, socket in scenario.students.items():
            await scenario.roster.add(socket, "student", lang)
        await scenario.pipeline.start()
        await asyncio.wait_for(scenario.pipeline._warmup_task, 10)
        if not scenario.pipeline.ready:
            raise RuntimeError("initial warmup failed")
        first_pid = runners[kind]._generation.process.pid
        await scenario.pipeline.set_accepting_audio(True)
        await scenario.feed(0)
        await asyncio.wait_for(scenario.pipeline.drain(timeout=None), 5)
        await callbacks()
        healthy = {lang: [frame["text"] for frame in socket.frames if frame["type"] == "caption"]
                   for lang, socket in scenario.students.items()}
        if healthy != {lang: [f"{lang}:授業の発話0です。"] for lang in scenario.students}:
            raise RuntimeError(f"healthy PCM did not reach both student languages: {healthy}")
        result["healthy_captions"] = healthy
        await scenario.feed(1)
        await observe((target / "operation").exists)
        await asyncio.wait_for(scenario.pipeline.drain(timeout=None), 5)
        require(not pid_exists(first_pid), "operation_recovery", "old process reaped", "alive", trace)
        await scenario.feed(2)
        await observe((target / "startup").exists)
        await asyncio.wait_for(scenario.pipeline.drain(timeout=None), 5)
        result["startup_pid"] = int((target / "startup").read_text(encoding="ascii"))
        blocked_pid = result["startup_pid"]
        result["startup_alive_after_audio_2"] = pid_exists(blocked_pid)
        if release:
            (target / "release-startup").touch()
            trace.append("release slow startup after short request deadlines")
            await observe(lambda: runners[kind]._generation.startup.done())
        else:
            trace.append("wait for startup deadline and child reaping without new requests")
            observation_deadline = time.monotonic() + STARTUP_TIMEOUT + 1
            while pid_exists(blocked_pid) and time.monotonic() < observation_deadline:
                await asyncio.sleep(0.01)
            result["reaped_without_new_requests"] = not pid_exists(blocked_pid)
        trace.append("feed audio 3 and 4 and observe both student languages")
        for identity in (3, 4):
            await scenario.feed(identity)
            await asyncio.wait_for(scenario.pipeline.drain(timeout=None), 5)
        await callbacks()
        expected = {lang: [f"{lang}:授業の発話{identity}です。" for identity in (3, 4)]
                    for lang in scenario.students}
        actual = {lang: [frame["text"] for frame in socket.frames
                         if frame["type"] == "caption" and frame["ja"] in
                         ("授業の発話3です。", "授業の発話4です。")]
                  for lang, socket in scenario.students.items()}
        result.update(expected=expected, actual=actual, startup_never_released=not release,
                      blocked_generation_alive=pid_exists(blocked_pid),
                      notices=[f["code"] for f in scenario.teacher.frames if f["type"] == "error"],
                      inputs=len(scenario.inputs), asr_calls=scenario.pipeline.asr_calls,
                      mt_calls=scenario.pipeline.captions.metrics().calls)
        if not release:
            require(result["reaped_without_new_requests"], "startup_recovery",
                    dict(reaped_without_new_requests=True, captions=expected),
                    dict(reaped_without_new_requests=False, captions=actual), trace)
        require(actual == expected, "startup_recovery", expected, actual, trace)
        if release:
            require(runners[kind]._generation.process.pid == blocked_pid, "startup_waiter_isolation",
                    "slow startup reused after release", "replacement generation", trace)
    except Violation as error:
        failure = error.detail
    finally:
        try:
            await asyncio.wait_for(scenario.close(), 5)
            result["pipeline_stop_returned"] = True
            result["child_pids"] = sorted(int(path.name.removeprefix("process-"))
                                          for stage in KINDS for path in (directory / stage).glob("process-*"))
            result["all_children_reaped"] = all(not pid_exists(pid) for pid in result["child_pids"])
            result["requests"] = sorted(
                [dict(stage=stage, **json.loads(line)) for stage in KINDS
                 for path in (directory / stage).glob("requests-*.jsonl")
                 for line in path.read_text(encoding="utf-8").splitlines()], key=lambda item: item["at"])
            if not result["all_children_reaped"]:
                failure = dict(property="startup_shutdown", expected="all children reaped",
                               actual="child alive", trace=trace)
        finally:
            for runner in runners.values():
                await emergency_cleanup(runner)
    result["status"] = "FAIL" if failure else "PASS"
    if failure:
        result["violation"] = failure
    return result


def child_run(source_root, suite, *, kind="asr", phase="warmup", release=False, single=False):
    with tempfile.TemporaryDirectory(prefix="inference-startup-") as temporary:
        directory = Path(temporary)
        destination = directory / "result.json"
        command = [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                   "--source-root", str(source_root), "--worker", suite, "--work-dir", str(directory),
                   "--kind", kind, "--phase", phase, "--json", str(destination)]
        if release:
            command.append("--release")
        if single:
            command.append("--single")
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                                 start_new_session=sys.platform != "win32")
        try:
            stdout, stderr = child.communicate(timeout=180 if suite == "bounded" else 40)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True)
            else:
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate(timeout=5)
            raise RuntimeError(f"{suite} probe exceeded its outer watchdog")
        if child.returncode not in (0, 1) or not destination.exists():
            raise RuntimeError(f"{suite} probe crashed: {stdout}\n{stderr}")
        result = json.loads(destination.read_text(encoding="utf-8"))
        if (child.returncode == 0) != (result["status"] == "PASS"):
            raise RuntimeError("probe exit code disagrees with its report")
        return result


MUTATIONS = (
    ("cancel_shared_startup", "startup_waiter_isolation",
     "await asyncio.shield(generation.startup)", "await generation.startup"),
    ("wrong_resumed_translation", "startup_resumption",
     "InferenceSucceeded(cast(T, reply.value),", "InferenceSucceeded(cast(T, 'wrong'),"),
    ("skip_startup_cleanup", "startup_shutdown",
     "self._close_task = self._retire(self._generation)",
     "self._close_task = asyncio.create_task(asyncio.sleep(0))"),
)


def mutations(source_root):
    reports = []
    for name, property_name, before, after in MUTATIONS:
        with tempfile.TemporaryDirectory(prefix="startup-mutation-") as temporary:
            scratch = Path(temporary)
            shutil.copytree(source_root / "server", scratch / "server",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = scratch / "server/inference_process.py"
            original = target.read_text(encoding="utf-8")
            if original.count(before) != 1:
                raise RuntimeError(f"mutation site changed: {name}")
            target.write_text(original.replace(before, after), encoding="utf-8")
            broken = child_run(scratch, "bounded", single=True)
            violation = next((c["violation"] for c in broken["violations"]
                              if c["violation"]["property"] == property_name), None)
            if violation is None:
                raise RuntimeError(f"mutation did not fail on {property_name}: {broken}")
            target.write_text(original, encoding="utf-8")
            restored = child_run(scratch, "bounded", single=True)
            if restored["status"] != "PASS":
                raise RuntimeError(f"restored bounded controls failed: {restored}")
            reports.append(dict(name=name, detected=True, restored="PASS", violation=violation))
    with tempfile.TemporaryDirectory(prefix="startup-deadline-mutation-") as temporary:
        scratch = Path(temporary)
        shutil.copytree(source_root / "server", scratch / "server",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        target = scratch / "server/inference_process.py"
        original = target.read_text(encoding="utf-8")
        before = "                deadline = asyncio.get_running_loop().time() + self._startup_timeout_s"
        if original.count(before) != 1:
            raise RuntimeError("startup deadline mutation site changed")
        cases = []
        for kind, phase in itertools.product(KINDS, PHASES):
            target.write_text(original.replace(before, "                deadline = float('inf')"),
                              encoding="utf-8")
            broken = child_run(scratch, "native", kind=kind, phase=phase)
            target.write_text(original, encoding="utf-8")
            restored = child_run(scratch, "native", kind=kind, phase=phase)
            if broken["status"] != "FAIL" or restored["status"] != "PASS" or (
                    broken["violation"]["property"] != "startup_recovery"):
                raise RuntimeError(f"startup deadline mutation failed: {broken}, {restored}")
            cases.append(dict(kind=kind, phase=phase, detected=True, restored="PASS",
                              violation=broken["violation"]))
        reports.append(dict(name="disable_startup_deadline", detected=True, restored="PASS", cases=cases))
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--worker", choices=("bounded", "native"), help=argparse.SUPPRESS)
    parser.add_argument("--work-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--kind", choices=KINDS, default="asr")
    parser.add_argument("--phase", choices=PHASES, default="warmup")
    parser.add_argument("--release", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--single", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "scripts/formal"))
    sys.path.insert(0, str(args.source_root.resolve()))
    logging.disable(logging.CRITICAL)
    try:
        if args.worker:
            if args.work_dir is None:
                parser.error("--worker requires --work-dir")
            report = asyncio.run(bounded(args.work_dir, args.single) if args.worker == "bounded" else
                                 native_case(args.work_dir, args.kind, args.phase, args.release))
        else:
            report = dict(sync_commit=SYNC_COMMIT, python=platform.python_version(),
                          scope="finite real-process schedules and synthetic PCM witnesses; no unbounded liveness proof",
                          bounded=child_run(args.source_root, "bounded"),
                          native=[child_run(args.source_root, "native", kind=kind, phase=phase, release=release)
                                  for kind, phase, release in itertools.product(KINDS, PHASES, (True, False))])
            if args.mutation_check:
                report["mutations"] = mutations(args.source_root)
            report["status"] = "FAIL" if report["bounded"]["status"] != "PASS" or any(
                case["status"] != "PASS" for case in report["native"]) else "PASS"
    except Exception as error:
        report = dict(status="ERROR", error=f"{type(error).__name__}: {error}")
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 2 if report["status"] == "ERROR" else int(report["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
