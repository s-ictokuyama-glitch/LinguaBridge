"""Bounded exploration of real lesson recording code, not an unbounded proof."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from concurrent.futures import Executor, Future
from dataclasses import asdict, dataclass
import itertools
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SYNC_COMMIT = "4efcb9f6edd2b1e39aea9c173d9b8b29870861f1"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--depth", type=int, default=4, choices=range(0, 7))
parser.add_argument("--suite", choices=("all", "commands", "pending", "save"), default="all")
parser.add_argument("--json", type=Path)
parser.add_argument("--source-root", type=Path, default=ROOT)
parser.add_argument("--mutation-check", action="store_true")
args = parser.parse_args()
sys.path.insert(0, str(args.source_root.resolve()))

import numpy as np

from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, RecordingConfig, VadConfig
from server.inference import InferenceRunner
from server.lesson import Lesson
from server.mt.fake_engine import FakeTranslationEngine
from server.pipeline import Pipeline
from server.recorder import SessionRecorder
from server.roster import Roster
from server.turn.assembler import Turn


@dataclass
class Violation:
    property: str
    trace: list[str]
    expected: list[list[str]]
    actual: list[list[str]]


class ControlledExecutor(Executor):
    def __init__(self):
        self.pending = []

    def submit(self, fn, /, *positional, **keywords):
        future = Future()
        future.set_running_or_notify_cancel()
        self.pending.append((future, fn, positional, keywords))
        return future

    def complete(self):
        future, fn, positional, keywords = self.pending.pop(0)
        future.set_result(fn(*positional, **keywords))


class Classroom:
    def __init__(self, directory, *, asr_executor=None):
        config = AppConfig(vad=VadConfig(engine="energy", threshold=300))
        config.recording = RecordingConfig(default_on=True, out_dir=str(directory))
        config.turn.strategy = "simple"
        config.limits.asr_timeout_s = 90
        self.pipeline = Pipeline(
            Roster(), config, FakeASREngine(), FakeTranslationEngine(["en"]),
            asr_runner=InferenceRunner(asr_executor, name="asr"),
        )
        self.pipeline._tasks = [asyncio.create_task(self.pipeline._asr_worker())]
        self.lesson = Lesson(self.pipeline, config, join_code="1234")
        self.directory = directory
        self.state = "idle"
        self.recording = True
        self.generation = 0
        self.expected = {}
        self.trace = []
        self.saved_bytes = {}
        self.serial = 0

    async def control(self, action):
        self.trace.append(action)
        await self.lesson.control(action)
        next_state = self.lesson.state
        if self.state == "ended" and next_state != "ended":
            self.generation += 1
        self.state = next_state
        self.assert_immutable()

    async def speak(self):
        self.trace.append("confirmed_turn")
        if self.state != "live":
            return
        self.serial += 1
        marker = f"lesson{self.generation}-turn{self.serial}"
        if self.recording:
            self.expected[marker] = self.generation
        turn = Turn(marker, 0, 0.4, 0, time.monotonic(), 1, "strong_end")
        await self.pipeline._publish_turn(turn, closed_at=turn.closed_at)
        self.assert_immutable()

    def toggle(self, enabled):
        self.trace.append("record_on" if enabled else "record_off")
        self.recording = enabled
        self.lesson.set_recording(enabled)

    def assert_immutable(self):
        current = {p: p.read_bytes() for p in self.directory.rglob("*") if p.is_file()}
        for path, contents in self.saved_bytes.items():
            if current.get(path) != contents:
                raise AssertionError(f"saved recording changed at {self.trace}: {path.name}")
        if (self.lesson._recording_task is None or self.lesson._recording_task.done()):
            self.saved_bytes = current

    def result(self):
        actual = sorted(
            sorted(json.loads(line)["ja"] for line in path.read_text(encoding="utf-8").splitlines())
            for path in self.directory.rglob("transcript.jsonl")
        )
        expected = sorted(
            sorted(marker for marker, owner in self.expected.items() if owner == generation)
            for generation in sorted(set(self.expected.values()))
        )
        if actual != expected:
            return Violation("recording_ownership", self.trace.copy(), expected, actual)
        return None

    async def close(self):
        await self.pipeline.stop()


async def command_case(commands, parent):
    with tempfile.TemporaryDirectory(dir=parent) as directory:
        room = Classroom(Path(directory))
        try:
            await room.control("start")
            await room.speak()
            for command in commands:
                if command == "speak":
                    await room.speak()
                elif command.startswith("record_"):
                    room.toggle(command == "record_on")
                else:
                    await room.control(command)
            await room.control("end")
            return room.result()
        finally:
            await room.close()


async def settle(predicate, pipeline):
    for _ in range(100):
        for task in pipeline._tasks:
            if task.done():
                task.result()
        if predicate():
            return
        await asyncio.sleep(0)
    raise RuntimeError("ASR barrier did not settle within 100 event-loop turns")


async def pending_case(schedule, restart, parent):
    with tempfile.TemporaryDirectory(dir=parent) as directory:
        executor = ControlledExecutor()
        room = Classroom(Path(directory), asr_executor=executor)
        pipeline = room.pipeline
        loop = asyncio.get_running_loop()
        loop_time = loop.time
        advance = 0.0
        timeouts = 0

        async def control_at_deadline(action):
            nonlocal advance, timeouts
            if not executor.pending:
                await room.control(action)
                return
            control = asyncio.create_task(room.control(action))
            await asyncio.sleep(0)  # Let control enter the real recording wait.
            if not control.done():
                advance += 30
                timeouts += 1
                if advance >= 90:
                    raise RuntimeError("recording waits reached the ASR deadline")
                room.trace.append("drain_timeout_with_ASR_pending")
            await control

        try:
            await room.control("start")
            await room.speak()
            room.trace.append("admit_old_audio")
            room.expected[FakeASREngine.PHRASES[1000]] = room.generation
            pcm = np.concatenate((np.full(6400, 1000, dtype=np.int16), np.zeros(19200, dtype=np.int16)))
            await pipeline.feed_audio(pcm.tobytes())
            await settle(lambda: len(executor.pending) == 1, pipeline)
            retry_restart = False
            retry_turn = False
            with patch.object(loop, "time", lambda: loop_time() + advance):
                for event in schedule:
                    if event == "asr_done":
                        room.trace.append(event)
                        executor.complete()
                        await settle(
                            lambda: pipeline._asr_backlog.is_idle and (room.lesson._recording_task is None or room.lesson._recording_task.done()),
                            pipeline,
                        )
                        if retry_restart:
                            room.trace.append("retry_restart_after_save")
                            await room.control(restart)
                            if restart == "pause":
                                await room.control("start")
                            if retry_turn:
                                await room.speak()
                    elif event == "restart":
                        await control_at_deadline(restart)
                        if executor.pending:
                            if room.lesson.state != "ended":
                                return Violation(
                                    "pending_restart_accepted", room.trace.copy(), [["ended"]], [[room.lesson.state]]
                                ), timeouts
                            room.trace.append("restart_refused_while_ASR_pending")
                            retry_restart = True
                        elif restart == "pause":
                            await room.control("start")
                    elif event == "next_turn":
                        if room.state == "ended":
                            room.trace.append("next_turn_deferred_until_restart")
                            retry_turn = True
                        else:
                            await room.speak()
                    else:
                        await control_at_deadline(event)
            await room.control("end")
            return room.result(), timeouts
        finally:
            await room.close()


async def save_case(restart, failure, parent):
    with tempfile.TemporaryDirectory(dir=parent) as directory:
        room = Classroom(Path(directory))
        try:
            await room.control("start")
            await room.speak()
            recorder = room.lesson._recorder
            method = "write" if failure == "before_write" else "_write_markdown"
            with patch.object(SessionRecorder, method, side_effect=OSError("disk unavailable")):
                for action in ("end", restart, "pause", "start"):
                    await room.control(action)
                    if room.lesson.state != "ended":
                        raise AssertionError(f"unsaved recording accepted {action}: {room.trace}")
                    if room.lesson._recorder is not recorder:
                        raise AssertionError(f"unsaved recording replaced: {room.trace}")
            room.saved_bytes = {}
            await room.control(restart)
            if restart == "pause":
                await room.control("start")
            await room.speak()
            await room.control("end")
            return room.result()
        finally:
            await room.close()


def mutation_check(parent):
    mutations = {
        "remove_recorder_rotation": (
            "                self._recorder = self._new_recorder()",
            "                pass",
            "commands",
            "recording_ownership",
        ),
        "skip_save_before_rotation": (
            '            if self.state == "ended" and action != "end":\n'
            "                await self._finalize_lesson()",
            '            if self.state == "ended" and action != "end":\n'
            "                pass",
            "save",
            "unsaved recording accepted",
        ),
        "seal_provisional_recording": (
            "            self._write_recording(final=False)",
            "            self._write_recording(final=True)",
            "pending",
            "recording_ownership",
        ),
    }
    results = []
    for name, (before, after, suite, diagnostic) in mutations.items():
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            scratch = Path(directory)
            shutil.copytree(args.source_root / "server", scratch / "server", ignore=shutil.ignore_patterns("__pycache__"))
            product = scratch / "server/lesson.py"
            source = product.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"mutation anchor changed: {name}; review the real code")
            product.write_text(source.replace(before, after), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--source-root", str(scratch), "--suite", suite, "--depth", "4"],
                capture_output=True, text=True, encoding="utf-8", timeout=120,
            )
            detected = result.returncode == 1 and diagnostic in result.stdout + result.stderr
            results.append({"mutation": name, "detected": detected})
            if not detected:
                raise RuntimeError(f"mutation escaped or harness failed: {name}\n{result.stdout}\n{result.stderr}")
    return results


async def explore(parent):
    failures = []
    counts = Counter()
    if args.suite in ("all", "commands"):
        alphabet = ("start", "pause", "end", "speak", "record_on", "record_off")
        for length in range(args.depth + 1):
            for trace in itertools.product(alphabet, repeat=length):
                result = await command_case(trace, parent)
                counts["commands"] += 1
                if result:
                    failures.append(asdict(result))
    if args.suite in ("all", "pending"):
        for schedule in itertools.permutations(("asr_done", "end", "restart", "next_turn")):
            if not schedule.index("end") < schedule.index("restart") < schedule.index("next_turn"):
                continue
            for restart in ("start", "pause"):
                result, timeouts = await pending_case(schedule, restart, parent)
                counts["pending"] += 1
                counts["injected_drain_timeouts"] += timeouts
                if result:
                    failures.append(asdict(result))
    if args.suite in ("all", "save"):
        for restart, failure in itertools.product(("start", "pause"), ("before_write", "after_jsonl")):
            result = await save_case(restart, failure, parent)
            counts["save"] += 1
            if result:
                failures.append(asdict(result))
    return {"counts": dict(counts), "violations": failures}


def main():
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="linguabridge-recording-") as directory:
        report = asyncio.run(explore(Path(directory)))
        if args.mutation_check:
            report["mutations"] = mutation_check(Path(directory))
    report["scope"] = {
        "depth": args.depth,
        "source_root": str(args.source_root.resolve()),
        "python": sys.version.split()[0],
        "pending_asr_timeout_s": 90,
        "drain_timeout_s": 30,
    }
    report["status"] = "FAIL" if report["violations"] else "PASS"
    report["sync_commit"] = SYNC_COMMIT
    output = json.dumps(report, ensure_ascii=True, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return int(bool(report["violations"]))


if __name__ == "__main__":
    raise SystemExit(main())
