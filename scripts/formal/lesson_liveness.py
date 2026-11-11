"""Bounded schedules and sustained PCM witnesses against the real translation pipeline."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from functools import partial
import itertools
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SYNC_COMMIT = "a15aea2288be88165acb3f9c9f4792d5a0a13f59"
OPERATIONS = ("feed", "asr_ok", "mt_ok", "asr_fail", "mt_fail")
WITNESSES = ("distinct", "repeated", "capacity", "asr_timeout", "mt_timeout", "held_drain")
CALLBACK_ROUNDS = 16


def japanese(identity):
    return f"授業の発話{identity}です。"


class Recognition:
    def warmup(self):
        pass

    def transcribe(self, pcm, sample_rate):
        from server.asr.base import ASRResult
        return ASRResult(japanese(int(pcm[pcm != 0][0]) - 1000))


class Translation:
    model_version = "formal-identity-v1"

    def warmup(self):
        pass

    def translate(self, text, lang):
        return f"{lang}:{text}"


@dataclass
class Job:
    future: Future
    operation: object

    @property
    def key(self):
        args = self.operation.args
        if self.operation.func.__name__ == "transcribe":
            pcm = args[0]
            return int(pcm[pcm != 0][0]) - 1000
        return args[:2]


class ControlledExecutor(Executor):
    def __init__(self):
        self.jobs = deque()
        self.closed = False

    def submit(self, fn, /, *args, **kwargs):
        if self.closed:
            raise RuntimeError("executor is stopped")
        operation = partial(fn, *args, **kwargs) if args or kwargs else fn
        future = Future()
        name = operation.func.__name__ if isinstance(operation, partial) else operation.__name__
        if name == "warmup":
            future.set_result(operation())
        else:
            self.jobs.append(Job(future, operation))
            self._start()
        return future

    def _start(self):
        while self.jobs and self.jobs[0].future.cancelled():
            self.jobs.popleft()
        if self.jobs and not self.jobs[0].future.running():
            self.jobs[0].future.set_running_or_notify_cancel()

    @property
    def current(self):
        self._start()
        return self.jobs[0] if self.jobs else None

    def complete(self, fail=False):
        job = self.jobs.popleft()
        if fail:
            job.future.set_exception(RuntimeError("injected inference failure"))
        else:
            try:
                value = job.operation()
            except Exception as error:
                job.future.set_exception(error)
            else:
                job.future.set_result(value)
        self._start()
        return job.key

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.closed = True
        if cancel_futures:
            for job in self.jobs:
                job.future.cancel()


class Socket:
    def __init__(self):
        self.frames = []
        self.closed = None

    async def send_json(self, payload):
        self.frames.append(payload)

    async def close(self, code=1000):
        self.closed = code


class Violation(Exception):
    def __init__(self, property_name, expected, actual, trace):
        self.detail = dict(property=property_name, expected=expected, actual=actual, trace=list(trace))
        super().__init__(property_name)


async def callbacks():
    for _ in range(CALLBACK_ROUNDS):
        await asyncio.sleep(0)


class Scenario:
    def __init__(self, *, defaults=False):
        from server.config import AppConfig
        from server.inference import InferenceRunner
        from server.pipeline import Pipeline
        from server.roster import Roster

        settings = {
            "vad": {"engine": "energy", "threshold": 300, "min_silence_ms": 100, "pre_roll_ms": 0},
            "turn": {"strategy": "simple"}, "partial": {"enabled": False},
            "monitoring": {"stats_interval_s": 3600},
        }
        if not defaults:
            settings.update(history_resend=3, mt={"cache_size": 2},
                            limits={"mt_queue_max": 1, "mt_replay_watermark": 1})
        self.config = AppConfig.model_validate(settings)
        self.roster = Roster(self.config.limits)
        self.asr = ControlledExecutor()
        self.mt = ControlledExecutor()
        self.pipeline = Pipeline(self.roster, self.config, Recognition(), Translation(),
                                 asr_runner=InferenceRunner(self.asr), mt_runner=InferenceRunner(self.mt))
        self.teacher = Socket()
        self.students = {lang: Socket() for lang in ("en", "zh")}
        self.inputs = []
        self.failed_asr = set()
        self.failed_mt = set()
        self.trace = []
        self.drain_task = None

    async def start(self):
        await self.roster.add(self.teacher, "teacher")
        for lang, socket in self.students.items():
            await self.roster.add(socket, "student", lang)
        await self.pipeline.start()
        await callbacks()
        if not self.pipeline.ready:
            raise RuntimeError("warmup did not settle within the callback bound")
        await self.pipeline.set_accepting_audio(True)
        await callbacks()

    def require(self, condition, property_name, expected, actual):
        if not condition:
            raise Violation(property_name, expected, actual, self.trace)

    def compare_sequence(self, property_name, expected, actual):
        if expected == actual:
            return
        index = next((i for i, pair in enumerate(zip(expected, actual)) if pair[0] != pair[1]),
                     min(len(expected), len(actual)))
        self.require(False, property_name,
                     dict(count=len(expected), first_difference=index,
                          item=expected[index] if index < len(expected) else None),
                     dict(count=len(actual), first_difference=index,
                          item=actual[index] if index < len(actual) else None))

    def workers_alive(self):
        workers = [*self.pipeline._tasks, self.pipeline.captions._worker_task]
        stopped = []
        for task in workers:
            if task is None or task.done():
                error = None if task is None or task.cancelled() else task.exception()
                stopped.append({"name": None if task is None else task.get_name(), "error": repr(error)})
        self.require(not stopped, "worker_liveness", "all workers remain active", stopped)

    def outstanding(self):
        return dict(asr=self.pipeline.asr_queue_depth,
                    audio_seconds=self.pipeline.audio_queue_seconds,
                    mt=self.pipeline.captions.queue_depth,
                    asr_running=self.asr.current is not None, mt_running=self.mt.current is not None)

    async def feed(self, identity=None):
        import numpy as np
        identity = max(self.inputs, default=0) + 1 if identity is None else identity
        self.inputs.append(identity)
        self.trace.append(f"feed_pcm({identity})")
        await self.pipeline.feed_audio(np.full(1600, 1000 + identity, dtype="<i2").tobytes())
        await self.pipeline.feed_audio(np.zeros(1600, dtype="<i2").tobytes())
        await callbacks()
        self.workers_alive()
        self.require(self.pipeline.asr_dropped_segments == 0, "audio_admission", 0,
                     self.pipeline.asr_dropped_segments)

    async def complete(self, kind, fail=False):
        executor = self.asr if kind == "asr" else self.mt
        if executor.current is None:
            return False
        key = executor.current.key
        self.trace.append(f"{kind}_{'fail' if fail else 'ok'}({key})")
        if fail:
            (self.failed_asr if kind == "asr" else self.failed_mt).add(key)
        executor.complete(fail)
        await callbacks()
        self.workers_alive()
        return True

    def expected(self, lang):
        good = [identity for identity in self.inputs if identity not in self.failed_asr]
        return [dict(seq=seq, ja=japanese(identity), text=f"{lang}:{japanese(identity)}", lang=lang)
                for seq, identity in enumerate(good, 1)
                if (japanese(identity), lang) not in self.failed_mt]

    def compare(self):
        expected_teacher = [dict(seq=seq, ja=japanese(identity)) for seq, identity in enumerate(
            (identity for identity in self.inputs if identity not in self.failed_asr), 1)]
        actual_teacher = [{key: frame[key] for key in ("seq", "ja")} for frame in self.teacher.frames
                          if frame["type"] == "asr_final"]
        self.compare_sequence("recognition_delivery", expected_teacher, actual_teacher)
        for lang, socket in self.students.items():
            actual = [{key: frame[key] for key in ("seq", "ja", "text", "lang")}
                      for frame in socket.frames if frame["type"] == "caption"]
            self.compare_sequence("caption_delivery", self.expected(lang), actual)
            self.require(socket.closed is None, "healthy_connection", None, socket.closed)

    async def finish(self):
        self.trace.append("drain_with_eventual_inference_completion")
        self.drain_task = asyncio.create_task(self.pipeline.drain(timeout=None))
        try:
            for _ in range(4 * (len(self.inputs) + 4)):
                await callbacks()
                self.workers_alive()
                remaining = self.outstanding()
                if self.drain_task.done():
                    await self.drain_task
                    self.require(not any(remaining.values()), "drain_completion", "no unfinished work", remaining)
                    await callbacks()
                    self.compare()
                    return
                for kind in ("asr", "mt"):
                    await self.complete(kind)
            self.require(False, "eventual_drain", "drain returns after released inference", self.outstanding())
        finally:
            if not self.drain_task.done():
                self.drain_task.cancel()
            await asyncio.gather(self.drain_task, return_exceptions=True)
            self.drain_task = None

    async def continuation(self):
        await self.feed()
        await self.finish()

    def screens(self, name):
        return [dict(name=name, language=lang, frames=socket.frames, expected=self.expected(lang))
                for lang, socket in self.students.items()]

    def summary(self):
        return dict(inputs=len(self.inputs), asr_calls=self.pipeline.asr_calls,
                    mt_calls=self.pipeline.captions.metrics().calls,
                    captions={lang: len(self.expected(lang)) for lang in self.students},
                    cache_hits=self.pipeline.captions.metrics().cache.hits,
                    history_size=len(self.pipeline.captions.history.since(0)))

    async def close(self):
        await self.pipeline.stop()


async def schedule(operations):
    scenario = Scenario()
    try:
        await scenario.start()
        for operation in operations:
            if operation == "feed":
                await scenario.feed()
            else:
                kind, outcome = operation.split("_")
                if not await scenario.complete(kind, outcome == "fail"):
                    return False
        await scenario.finish()
        await scenario.continuation()
        return True
    finally:
        await scenario.close()


async def witness(name, screens):
    scenario = Scenario(defaults=name in ("distinct", "repeated", "capacity"))
    try:
        await scenario.start()
        if name in ("distinct", "repeated"):
            for identity in range(1, 601 if name == "distinct" else 65):
                await scenario.feed(identity if name == "distinct" else 1)
                await scenario.finish()
            if name == "repeated":
                scenario.require(scenario.pipeline.captions.metrics().cache.hits == 126,
                                 "cache_path", 126, scenario.pipeline.captions.metrics().cache.hits)
        elif name == "capacity":
            for _ in range(140):
                await scenario.feed()
                await scenario.complete("asr")
            scenario.require(scenario.pipeline.captions.queue_depth == 256, "capacity_reached", 256,
                             scenario.pipeline.captions.queue_depth)
            scenario.require(any(frame.get("code") == "mt_backlog" for frame in scenario.teacher.frames),
                             "backpressure_notice", "mt_backlog", scenario.teacher.frames[-4:])
            await scenario.finish()
        elif name in ("asr_timeout", "mt_timeout"):
            kind = name.split("_")[0]
            setattr(scenario.config.limits, f"{kind}_timeout_s", 0)
            await scenario.feed()
            if kind == "mt":
                await scenario.complete("asr")
                scenario.failed_mt.update((japanese(1), lang) for lang in ("en", "zh"))
            else:
                scenario.failed_asr.add(1)
            executor = scenario.asr if kind == "asr" else scenario.mt
            scenario.require(executor.current is not None and executor.current.future.running(),
                             "timeout_running", "native future remains running", scenario.outstanding())
            scenario.require(any(frame.get("code") == name for frame in scenario.teacher.frames),
                             "timeout_notice", name, scenario.teacher.frames[-4:])
            setattr(scenario.config.limits, f"{kind}_timeout_s", 60)
            executor.complete()
            await callbacks()
            await scenario.finish()
        elif name == "held_drain":
            await scenario.feed()
            await scenario.complete("asr")
            await scenario.finish()
        await scenario.continuation()
        screens.extend(scenario.screens(name))
        return scenario.summary()
    finally:
        await scenario.close()


async def check(depth, case=None, only_witness=None):
    attempted = legal = 0
    violations = {}
    witnesses = {}
    screens = []
    sequences = [] if only_witness else ([tuple(case.split(","))] if case is not None else (
        seq for size in range(depth + 1) for seq in itertools.product(OPERATIONS, repeat=size)))
    for sequence in sequences:
        if any(operation not in OPERATIONS for operation in sequence):
            raise ValueError("unknown operation")
        attempted += 1
        try:
            async with asyncio.timeout(5):
                legal += bool(await schedule(sequence))
        except Violation as error:
            violations.setdefault(error.detail["property"], error.detail)
    if case is not None and not legal and not violations:
        raise ValueError("case contains an operation without a pending inference")
    for name in ([only_witness] if only_witness else WITNESSES if case is None else []):
        try:
            async with asyncio.timeout(30):
                witnesses[name] = await witness(name, screens)
        except Violation as error:
            error.detail["witness"] = name
            violations.setdefault(error.detail["property"], error.detail)
    return dict(status="FAIL" if violations else "PASS", sync_commit=SYNC_COMMIT,
                depth=depth if case is None and not only_witness else None,
                callback_rounds=CALLBACK_ROUNDS, attempted=attempted, legal=legal,
                witnesses=witnesses, violations=list(violations.values())), screens


MUTATIONS = (
    ("stop_asr_after_16", "worker_liveness", "distinct", "server/pipeline.py",
     "await self._process_segment(event)",
     "await self._process_segment(event)\n                        if self.asr_calls >= 16:\n                            return"),
    ("lose_mt_wakeup", "eventual_drain", "held_drain", "server/caption_delivery.py",
     "        self._mt_work.set()\n        return True", "        return True"),
    ("drop_full_live_job", "caption_delivery", "capacity", "server/caption_delivery.py",
     "                await self._mt_queue.put(entry)", "                return False"),
    ("drain_before_mt", "drain_completion", "held_drain", "server/pipeline.py",
     "        await self.captions.drain(timeout)", "        return"),
    ("wrong_translation", "caption_delivery", "held_drain", "server/caption_delivery.py",
     "text=translated.text,", 'text="wrong translation",'),
    ("wrong_recognition", "recognition_delivery", "held_drain", "server/caption_delivery.py",
     "text_ja=turn.text,", 'text_ja="wrong recognition",'),
)


def mutation_check(source_root):
    results = []
    for name, property_name, witness_name, relative, before, after in MUTATIONS:
        healthy, _ = asyncio.run(check(0, only_witness=witness_name))
        if healthy["status"] != "PASS":
            raise RuntimeError(f"mutation witness is not healthy: {name}")
        with tempfile.TemporaryDirectory(prefix="lesson-liveness-") as directory:
            scratch = Path(directory)
            shutil.copytree(source_root / "server", scratch / "server",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = scratch / relative
            source = target.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"mutation site changed: {name}")
            target.write_text(source.replace(before, after), encoding="utf-8")
            child = subprocess.run(
                [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                 "--source-root", str(scratch), "--witness", witness_name],
                cwd=scratch, capture_output=True, encoding="utf-8", timeout=60,
            )
            if child.returncode != 1:
                raise RuntimeError(f"mutation did not report a violation: {name}\n{child.stderr}\n{child.stdout}")
            report = json.loads(child.stdout)
            violation = next((v for v in report["violations"] if v["property"] == property_name), None)
            if violation is None:
                raise RuntimeError(f"mutation missed {property_name}: {name}\n{child.stdout}")
            results.append(dict(name=name, healthy="PASS", detected=True, violation=violation))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--depth", type=int, default=6)
    parser.add_argument("--case")
    parser.add_argument("--witness", choices=WITNESSES)
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--screens", type=Path)
    args = parser.parse_args()
    if not 0 <= args.depth <= 6:
        parser.error("depth must be between 0 and 6")
    if args.case is not None and args.witness:
        parser.error("choose --case or --witness")
    if args.json and args.screens and args.json.resolve() == args.screens.resolve():
        parser.error("--json and --screens must be different paths")
    sys.path.insert(0, str(args.source_root.resolve()))
    logging.disable(logging.CRITICAL)
    report, screens = asyncio.run(check(args.depth, args.case, args.witness))
    if args.mutation_check and report["status"] == "PASS":
        report["mutations"] = mutation_check(args.source_root.resolve())
    output = json.dumps(report, ensure_ascii=False, indent=2)
    for path, value in ((args.json, output), (args.screens, json.dumps(screens, ensure_ascii=False))):
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value + "\n", encoding="utf-8")
    print(output)
    return int(report["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
