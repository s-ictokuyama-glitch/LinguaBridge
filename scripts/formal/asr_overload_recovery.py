"""Check classroom caption continuity when the ASR audio budget is exhausted."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from itertools import product
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.formal.lesson_liveness import ControlledExecutor, Socket, Translation, Violation, callbacks
from server.asr.base import ASRResult
from server.config import load_config
from server.inference import InferenceRunner
from server.pipeline import Pipeline
from server.roster import Roster


RELEASE = "release"
SHIPPING = (10, 10, 10, 10, RELEASE, 10, 10)
SILENCE = bytes(3200)


def clause(identity):
    # A continuing clause needs a real TurnBoundary before it can be translated.
    return f"条件{identity}が異なるので"


class Recognition:
    def warmup(self):
        pass

    def transcribe(self, pcm, sample_rate):
        marker, = np.unique(pcm[pcm != 0])
        return ASRResult(clause(int(marker) - 1000))


def schedules():
    for sizes in product((1, 2), repeat=4):
        for gaps in product((False, True), repeat=3):
            steps = [sizes[0]]
            for release, size in zip(gaps, sizes[1:], strict=True):
                if release:
                    steps.append(RELEASE)
                steps.append(size)
            yield 3, tuple(steps)
    yield 30, SHIPPING


async def classroom(config, capacity, steps):
    config = config.model_copy(deep=True)
    config.limits.asr_queue_seconds = capacity
    asr = ControlledExecutor()
    roster = Roster(config.limits)
    pipeline = Pipeline(roster, config, Recognition(), Translation(), asr_runner=InferenceRunner(asr))
    teacher = Socket()
    students = {lang: Socket() for lang in ("en", "zh")}
    pending = deque()
    accepted, dropped, trace = [], [], []
    dropped_seconds = 0
    tick = 0
    warning_ticks = []

    def compare(name, expected, actual):
        if actual != expected:
            raise Violation(name, expected, actual, trace)

    async def feed(frame, count):
        nonlocal tick
        for _ in range(count):
            tick += 1
            await pipeline.feed_audio(frame)

    async def offer(seconds):
        nonlocal dropped_seconds
        identity = len(accepted) + len(dropped) + 1
        trace.append(f"pcm({identity}, {seconds}s)")
        # 200 ms pre-roll + voice + the 500 ms needed to close the segment.
        voice_ticks = 10 * seconds - 7
        close_tick = tick + voice_ticks + 5
        if pending and sum(size for _, size in pending) + seconds > capacity:
            dropped.append(identity)
            dropped_seconds += seconds
            if not warning_ticks or close_tick - warning_ticks[-1] >= 100:
                warning_ticks.append(close_tick)
        else:
            accepted.append(identity)
            pending.append((identity, seconds))
        await feed(np.full(1600, 1000 + identity, dtype="<i2").tobytes(), voice_ticks)
        await feed(SILENCE, 10)
        await callbacks()
        compare("dropped_audio", [len(dropped), dropped_seconds],
                [pipeline.asr_dropped_segments, pipeline.asr_dropped_seconds])
        compare("teacher_notice", len(warning_ticks),
                sum(frame.get("code") == "audio_dropped" for frame in teacher.frames))

    async def release():
        identity, seconds = pending.popleft()
        trace.append(f"release({identity})")
        job = asr.current
        compare("asr_input", [identity, seconds * 16000],
                None if job is None else [job.key, job.operation.args[0].size])
        asr.complete()
        await callbacks()

    async def drain_and_compare():
        while pending:
            await release()
        if asr.current is not None:
            compare("asr_input", None, asr.current.key)
        await pipeline.drain(timeout=5)
        await asyncio.wait_for(asyncio.gather(*(s._queue.join() for s in roster._senders.values())), 5)
        compare("drained_audio", 0, pipeline.audio_queue_seconds)
        finals = [(seq, clause(i)) for seq, i in enumerate(accepted, 1)]
        compare("teacher_finals", finals,
                [(m["seq"], m["ja"]) for m in teacher.frames if m["type"] == "asr_final"])
        for lang, socket in students.items():
            compare(f"captions_{lang}", [(seq, ja, lang, f"{lang}:{ja}") for seq, ja in finals],
                    [(m["seq"], m["ja"], m["lang"], m["text"]) for m in socket.frames
                     if m["type"] == "caption"])

    # Advance the pipeline's notice clock with the PCM. Leave asyncio deadlines
    # on wall time so a stalled worker cannot stop the verifier's watchdog.
    with patch("server.pipeline.time", SimpleNamespace(monotonic=lambda: 10000 + tick / 10)):
        try:
            await roster.add(teacher, "teacher")
            for lang, socket in students.items():
                await roster.add(socket, "student", lang)
            await pipeline.start()
            await asyncio.wait_for(pipeline._warmup_task, 5)
            if not pipeline.ready:
                raise RuntimeError("fixture warmup failed")
            await pipeline.set_accepting_audio(True)
            await feed(SILENCE, 10)
            for step in steps:
                if step == RELEASE:
                    await release()
                else:
                    await offer(step)
            await drain_and_compare()
            trace.append("after drain")
            await offer(1)
            await drain_and_compare()
        finally:
            await pipeline.stop()
    return dict(accepted=accepted, dropped=dropped, dropped_seconds=dropped_seconds,
                teacher_notices=len(warning_ticks), captions_per_language=len(accepted))


async def check(cases):
    config = load_config(ROOT / "config.yaml")
    if (config.limits.asr_queue_seconds != 30 or config.turn.strategy != "morph"
            or config.turn.classifier != "surface" or config.partial.enabled):
        raise ValueError("review input recipes and oracle against the shipping configuration")
    config.vad.engine = "energy"
    config.vad.threshold = 300
    passed = failed = overload_cases = 0
    shipping = None
    counterexamples = {}
    for capacity, steps in cases:
        try:
            observation = await asyncio.wait_for(classroom(config, capacity, steps), timeout=15)
        except Violation as finding:
            failed += 1
            counterexamples.setdefault(finding.detail["property"],
                                       dict(capacity=capacity, steps=steps, **finding.detail))
        else:
            passed += 1
            overload_cases += bool(observation["dropped"])
            if steps == SHIPPING and capacity == 30:
                shipping = observation
    return dict(status="FAIL" if failed else "PASS", passed=passed, failed=failed,
                passing_cases_with_drops=overload_cases, shipping=shipping,
                first_counterexamples=list(counterexamples.values()))


MUTATIONS = (
    ("forget_active", "asr_backlog.py", "self._waiting_samples + self._active_samples",
     "self._waiting_samples", "dropped_audio"),
    ("drop_at_equality", "asr_backlog.py", "> self._max_audio_seconds",
     ">= self._max_audio_seconds", "dropped_audio"),
    ("never_readmit", "asr_backlog.py", "if samples and total_samples and (",
     "if (samples and self.dropped_segments) or samples and total_samples and (", "dropped_audio"),
    ("trim_asr_input", "pipeline.py", "partial(self._asr.transcribe, segment.pcm,",
     "partial(self._asr.transcribe, segment.pcm[1:],", "asr_input"),
    ("retain_charge", "pipeline.py", "return self._asr_backlog.total_seconds",
     "return self._asr_backlog.total_seconds + self._asr_backlog.dropped_seconds", "drained_audio"),
    ("lose_notice", "pipeline.py", "if outcome is Dropped and isinstance(event, Segment):",
     "if False:", "teacher_notice"),
    ("lose_boundary", "pipeline.py", "                        await self._close_pending_turn(event)",
     "                        if not self.asr_dropped_segments:\n"
     "                            await self._close_pending_turn(event)", "teacher_finals"),
    ("skip_chinese", "caption_delivery.py", "if self.roster.send(client, captions[selected]):",
     'if (selected != "zh" or job.utterance.seq < 5) and self.roster.send(client, captions[selected]):',
     "captions_zh"),
)


def mutation_check():
    results = {}
    with tempfile.TemporaryDirectory(prefix="asr-overload-") as directory:
        scratch = Path(directory)
        shutil.copytree(ROOT / "server", scratch / "server", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "config.yaml", scratch)
        scripts = scratch / "scripts/formal"
        scripts.mkdir(parents=True)
        for file in (Path(__file__), ROOT / "scripts/formal/lesson_liveness.py"):
            shutil.copy2(file, scripts)
        for name, file, before, after, expected in MUTATIONS:
            target = scratch / "server" / file
            source = target.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise ValueError(f"review mutation {name} in {file}")
            target.write_text(source.replace(before, after), encoding="utf-8")
            child = subprocess.run(
                [sys.executable, "-I", "-X", "utf8", "-B", str(scripts / Path(__file__).name), "--shipping"],
                cwd=scratch, capture_output=True, encoding="utf-8", timeout=30,
            )
            target.write_text(source, encoding="utf-8")
            if child.returncode != 1:
                raise RuntimeError(f"{name}: expected a counterexample; {child.stdout} {child.stderr}")
            findings = json.loads(child.stdout)["first_counterexamples"]
            if not any(f["property"] == expected for f in findings):
                raise RuntimeError(f"{name}: expected {expected}, got {findings}")
            results[name] = next(f for f in findings if f["property"] == expected)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--shipping", action="store_true", help="only the 30-second classroom case")
    group.add_argument("--case", metavar="STEPS", help="3-second budget, e.g. 1,2,1,release,1")
    group.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    cases = list(schedules())
    if args.shipping:
        cases = [(30, SHIPPING)]
    if args.case is not None:
        cases = [(cap, steps) for cap, steps in cases
                 if cap == 3 and ",".join(map(str, steps)) == args.case]
        if not cases:
            parser.error("use four sizes (1 or 2), optionally separated by release steps")
    logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(check(cases))
        if args.mutation_check and report["status"] == "PASS":
            mutations = mutation_check()
            if asyncio.run(check(cases)) != report:
                raise RuntimeError("restored full search differs from baseline")
            report.update(mutations=mutations, restored_matches=True)
    except Exception as error:
        report = dict(status="ERROR", error=f"{type(error).__name__}: {error}")
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return {"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
