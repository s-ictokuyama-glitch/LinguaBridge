"""Enumerate morph PCM schedules and check uninterrupted classroom caption delivery."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import itertools
import json
import logging
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SYNC_COMMIT = "1840857a512024404c3504cbe4465afb5d952c45"
sys.path.insert(0, str(Path(__file__).parent))
from lesson_endurance import Clock, TimedExecutor, clock_scope
from lesson_liveness import ControlledExecutor, Socket, Translation, Violation, callbacks

OPERATIONS = ("clause", "sentence", "empty", "boundary", "flush", "asr", "fail_asr", "mt")
WITNESSES = ("boundary", "flush", "backpressure", "count", "span")


@dataclass(frozen=True)
class Fragment:
    identity: int
    text: str
    ends_turn: bool


class Recognition:
    def __init__(self, fragments):
        self.fragments = fragments

    def warmup(self):
        pass

    def transcribe(self, pcm, sample_rate):
        import numpy as np
        from server.asr.base import ASRResult

        values = np.unique(pcm[pcm != 0])
        if len(values) != 1:
            raise RuntimeError(f"PCM fragment identity is ambiguous: {values}")
        return ASRResult(self.fragments[int(values[0]) - 1000].text)


class LessonCase:
    def __init__(self, *, clock=None, asr_ticks=1, mt_ticks=2):
        from server.config import AppConfig
        from server.inference import InferenceRunner
        from server.pipeline import Pipeline
        from server.roster import Roster

        self.config = AppConfig.model_validate({
            "vad": {"engine": "energy", "threshold": 300},
            "turn": {"strategy": "morph", "classifier": "surface"},
            "partial": {"enabled": False},
            "monitoring": {"stats_interval_s": 3600},
            "limits": {"mt_queue_max": 1, "mt_replay_watermark": 1},
        })
        self.fragments = {}
        self.events = []
        self.failed = set()
        self.trace = []
        self.clock = clock
        self.asr = ControlledExecutor() if clock is None else TimedExecutor(clock, asr_ticks)
        self.mt = ControlledExecutor() if clock is None else TimedExecutor(clock, mt_ticks)
        self.roster = Roster(self.config.limits)
        self.pipeline = Pipeline(self.roster, self.config, Recognition(self.fragments), Translation(),
                                 asr_runner=InferenceRunner(self.asr), mt_runner=InferenceRunner(self.mt))
        self.teacher = Socket()
        self.students = {lang: Socket() for lang in ("en", "zh")}

    def require(self, condition, name, expected, actual):
        if not condition:
            raise Violation(name, expected, actual, self.trace)

    async def start(self):
        await self.roster.add(self.teacher, "teacher")
        for lang, socket in self.students.items():
            await self.roster.add(socket, "student", lang)
        await self.pipeline.start()
        await callbacks()
        if not self.pipeline.ready:
            raise RuntimeError("warmup did not settle within 16 callback rounds")
        await self.pipeline.set_accepting_audio(True)

    async def close(self):
        await self.pipeline.stop()

    def fragment(self, kind):
        identity = len(self.fragments) + 1
        text = "" if kind == "empty" else f"【{identity}】" + (
            "温度が高いので" if kind == "clause" else "体積が増えます。")
        fragment = Fragment(identity, text, kind == "sentence")
        self.fragments[identity] = fragment
        self.events.append(fragment)
        return identity

    async def pcm(self, identity, ticks):
        import numpy as np
        data = np.full(1600, 1000 + identity if identity else 0, dtype="<i2").tobytes()
        for _ in range(ticks):
            await self.pipeline.feed_audio(data)
            await callbacks()

    async def flush(self):
        await self.pipeline.set_accepting_audio(False)
        await self.pipeline.flush_audio()
        await self.pipeline.set_accepting_audio(True)
        self.events.append("flush")
        await callbacks()

    async def step(self, operation, *, voice_ticks=10):
        self.trace.append(operation)
        if operation in ("clause", "sentence", "empty"):
            await self.pcm(self.fragment(operation), voice_ticks)
            await self.pcm(0, 5)
        elif operation == "boundary":
            await self.pcm(0, 5)
            self.events.append("boundary")
        elif operation == "flush":
            await self.flush()
        else:
            executor = self.mt if operation == "mt" else self.asr
            if executor.current is None:
                return False
            if operation == "fail_asr":
                self.failed.add(executor.current.key)
            executor.complete(fail=operation == "fail_asr")
            await callbacks()
        self.workers_alive()
        return True

    def workers_alive(self):
        workers = [*self.pipeline._tasks, self.pipeline.captions._worker_task]
        stopped = [t.get_name() for t in workers if t is not None and t.done()]
        self.require(not stopped and all(t is not None for t in workers),
                     "worker_liveness", "ASR, MT and stats workers alive", stopped)

    def expected(self):
        turns, pending = [], []
        for event in self.events:
            if isinstance(event, Fragment):
                if event.identity in self.failed or not event.text:
                    continue
                pending.append(event.text)
                if not event.ends_turn:
                    continue
            if pending:
                turns.append("".join(pending))
                pending = []
        return turns

    def compare(self):
        expected = [(i, text) for i, text in enumerate(self.expected(), 1)]
        actual = [(f["seq"], f["ja"]) for f in self.teacher.frames if f["type"] == "asr_final"]
        self.compare_sequence("morph_turns", expected, actual)
        for lang, socket in self.students.items():
            wanted = [(seq, text, f"{lang}:{text}", lang) for seq, text in expected]
            actual = [(f["seq"], f["ja"], f["text"], f["lang"])
                      for f in socket.frames if f["type"] == "caption"]
            self.compare_sequence("bilingual_delivery", wanted, actual)
            self.require(socket.closed is None, "bilingual_delivery", "connection open", socket.closed)

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

    async def settle(self):
        drain = asyncio.create_task(self.pipeline.drain(timeout=None))
        try:
            for _ in range(6 * (len(self.fragments) + 2)):
                await callbacks()
                self.workers_alive()
                if drain.done():
                    await drain
                    self.require(self.asr.current is None and self.mt.current is None
                                 and self.pipeline.queue_depth == 0,
                                 "drain_completion", "no queued or active inference", "work remains")
                    await callbacks()
                    self.compare()
                    return
                for executor in (self.asr, self.mt):
                    if executor.current is not None:
                        executor.complete()
            self.require(False, "drain_completion", "drain completes after inference release", "pending")
        finally:
            if not drain.done():
                drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)

    def summary(self):
        return dict(fragments=len(self.fragments), turns=len(self.expected()),
                    failed_asr=len(self.failed), asr_calls=self.pipeline.asr_calls,
                    mt_calls=self.pipeline.captions.metrics().calls,
                    captions={lang: sum(f["type"] == "caption" for f in socket.frames)
                              for lang, socket in self.students.items()})


async def schedule(operations):
    case = LessonCase()
    try:
        await case.start()
        for operation in operations:
            if not await case.step(operation):
                return False
        await case.settle()
        await case.step("flush")
        await case.settle()
        await case.step("sentence")
        await case.settle()
        case.require(case.pipeline.asr_dropped_segments == 0,
                     "audio_admission", 0, case.pipeline.asr_dropped_segments)
        return True
    finally:
        await case.close()


async def witness(name):
    case = LessonCase()
    try:
        await case.start()
        if name in ("boundary", "flush"):
            await case.step("clause")
            await case.step(name)
        elif name == "backpressure":
            for _ in range(4):
                await case.step("clause")
                await case.step("boundary")
                if case.asr.current is not None:
                    await case.step("asr")
            case.require(case.pipeline.captions.queue_depth == 1 and case.mt.current is not None
                         and any(f.get("code") == "mt_backlog" for f in case.teacher.frames),
                         "backpressure_reached", "full MT queue and warning", case.pipeline.queue_depth)
        else:
            count, ticks = (12, 10) if name == "count" else (9, 30)
            for _ in range(count):
                await case.step("clause", voice_ticks=ticks)
                await case.step("asr")
            case.events.append("configured_limit")
        await case.settle()
        await case.step("sentence")
        await case.settle()
        case.require(case.pipeline.asr_dropped_segments == 0,
                     "audio_admission", 0, case.pipeline.asr_dropped_segments)
        return dict(status="PASS", **case.summary())
    finally:
        await case.close()


async def sustained(seconds, asr_ticks, mt_ticks):
    import numpy as np

    clock = Clock()
    case = LessonCase(clock=clock, asr_ticks=asr_ticks, mt_ticks=mt_ticks)
    deadlines = []
    pcm_frames = 0
    peaks = dict(audio_seconds=0, asr_events=0, mt_jobs=0, pending_parts=0,
                 history=0, cache=0, receipts=0, tasks=0)
    voice = bytes(3200)
    with clock_scope(clock):
        try:
            await case.start()
            task_bound = len(asyncio.all_tasks()) + 4
            for tick in range(seconds * 10 + 100):
                clock.tick = tick
                case.trace = [f"PCM {min(tick, seconds * 10) / 10}s",
                              f"ASR {asr_ticks / 10}s, MT {mt_ticks / 10}s per language"]
                case.asr.release_due()
                case.mt.release_due()
                await callbacks()
                if tick < seconds * 10:
                    phase = tick % 100
                    kinds = {0: "clause", 15: "sentence", 40: "clause", 60: "clause", 75: "sentence"}
                    if phase == 75:
                        await case.flush()
                        deadlines.append((tick + 80, len(case.expected())))
                    if phase in kinds:
                        identity = case.fragment(kinds[phase])
                        voice = np.full(1600, 1000 + identity, dtype="<i2").tobytes()
                    speaking = phase < 10 or 15 <= phase < 25 or 40 <= phase < 50 or 60 <= phase < 70 or 75 <= phase < 90
                    await case.pipeline.feed_audio(voice if speaking else bytes(3200))
                    pcm_frames += 1
                    await callbacks()
                    if phase == 59:
                        case.events.append("boundary")
                    if phase in (29, 59, 94):
                        deadlines.append((tick + 80, len(case.expected())))
                case.workers_alive()
                p = case.pipeline
                sizes = dict(audio_seconds=p.audio_queue_seconds, asr_events=p.asr_queue_depth,
                             mt_jobs=p.captions.queue_depth, pending_parts=len(p._assembler._pending.parts),
                             history=len(p.captions.history.since(0)), cache=p.captions.metrics().cache.size,
                             receipts=max(len(c.caption_languages) for c in case.roster.students()),
                             tasks=len(asyncio.all_tasks()))
                bounds = dict(audio_seconds=30, asr_events=8, mt_jobs=1, pending_parts=12,
                              history=50, cache=512, receipts=100, tasks=task_bound)
                case.require(all(sizes[k] <= bounds[k] for k in sizes),
                             "retained_state_bound", bounds, sizes)
                for key, value in sizes.items():
                    peaks[key] = max(peaks[key], value)
                case.require(p.asr_dropped_segments == 0, "audio_admission", 0, p.asr_dropped_segments)
                while deadlines and deadlines[0][0] <= tick:
                    _, count = deadlines.pop(0)
                    for lang, socket in case.students.items():
                        delivered = sum(f["type"] == "caption" for f in socket.frames)
                        case.require(delivered >= count, "sustained_deadline",
                                     dict(lang=lang, at_least=count), delivered)
            case.compare()
            case.require(not deadlines and case.pipeline.queue_depth == 0
                         and case.asr.current is None and case.mt.current is None
                         and not case.pipeline._assembler.has_pending,
                         "drain_completion", "all input settled without a final flush", "unfinished work")
            return dict(status="PASS", seconds=seconds, pcm_frames=pcm_frames, asr_service_s=asr_ticks / 10,
                        mt_service_s=mt_ticks / 10, peaks=peaks, **case.summary())
        finally:
            await case.close()


async def check(depth=4, only_witness=None, seconds=3000, bounded_only=False, only_sustained=False):
    attempted = legal = failed = 0
    violations = {}
    witnesses = {}
    lessons = []
    def record(error):
        violations.setdefault(error.detail["property"], error.detail)
    if not only_witness and not only_sustained:
        for size in range(depth + 1):
            for operations in itertools.product(OPERATIONS, repeat=size):
                attempted += 1
                try:
                    legal += bool(await schedule(operations))
                except Violation as error:
                    failed += 1
                    record(error)
    for name in ([only_witness] if only_witness else () if only_sustained else WITNESSES):
        try:
            witnesses[name] = await witness(name)
        except Violation as error:
            error.detail["witness"] = name
            witnesses[name] = dict(status="FAIL")
            record(error)
    if not only_witness and not bounded_only:
        for asr_ticks, mt_ticks in ((1, 2), (8, 8)):
            try:
                lessons.append(await sustained(seconds, asr_ticks, mt_ticks))
            except Violation as error:
                error.detail["service_ticks"] = [asr_ticks, mt_ticks]
                record(error)
    return dict(status="FAIL" if violations else "PASS", sync_commit=SYNC_COMMIT,
                python=platform.python_version(), depth=depth, attempted=attempted,
                legal_passed=legal, failed=failed, witnesses=witnesses,
                sustained=lessons, violations=list(violations.values()))


MUTATIONS = (
    ("lose_boundary", "morph_turns", "boundary", "server/pipeline.py",
     "else self._assembler.on_boundary()", "else None"),
    ("boundary_before_asr", "morph_turns", "boundary", "server/pipeline.py",
     "            await self._enqueue_asr(event)",
     "            if isinstance(event, TurnBoundary):\n                await self._close_pending_turn(event)\n            else:\n                await self._enqueue_asr(event)"),
    ("lose_flush", "morph_turns", "flush", "server/pipeline.py",
     "        await self._enqueue_asr(FlushTurn(closed_at=time.monotonic()))", "        pass"),
    ("drop_full_mt", "bilingual_delivery", "backpressure", "server/caption_delivery.py",
     "                await self._mt_queue.put(entry)", "                return False"),
    ("skip_mt_drain", "drain_completion", "boundary", "server/pipeline.py",
     "        await self.captions.drain(timeout)", "        pass"),
    ("stop_on_boundary", "worker_liveness", "boundary", "server/pipeline.py",
     "                        await self._close_pending_turn(event)", "                        return"),
    ("late_boundary", "sustained_deadline", "sustained", "server/audio/vad.py",
     "if self._post_silence_run < self._force_silence_frames:",
     "if self._post_silence_run < self._force_silence_frames + 100:"),
    ("unbounded_history", "retained_state_bound", "sustained", "server/caption_history.py",
     "deque(maxlen=maxlen)", "deque()"),
    ("reject_pcm", "audio_admission", "sustained", "server/asr_backlog.py",
     "        total_samples = self._waiting_samples + self._active_samples",
     "        if samples:\n            self.dropped_segments += 1\n            return Dropped\n        total_samples = self._waiting_samples + self._active_samples"),
)


def mutation_check(source_root):
    results = []
    for name, property_name, probe, relative, before, after in MUTATIONS:
        options = dict(only_sustained=True, seconds=150) if probe == "sustained" else dict(only_witness=probe)
        healthy = asyncio.run(check(**options))
        if healthy["status"] != "PASS":
            raise RuntimeError(f"mutation control is not healthy: {name}: {healthy}")
        with tempfile.TemporaryDirectory(prefix="morph-lesson-") as directory:
            scratch = Path(directory)
            shutil.copytree(source_root / "server", scratch / "server",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = scratch / relative
            source = target.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"mutation site changed: {name}")
            target.write_text(source.replace(before, after), encoding="utf-8")
            args = ["--sustained-only", "--seconds", "150"] if probe == "sustained" else ["--witness", probe]
            child = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                                    "--source-root", str(scratch), *args], cwd=scratch,
                                   capture_output=True, encoding="utf-8", timeout=90)
            if child.returncode != 1:
                raise RuntimeError(f"mutation did not return a violation: {name}\n{child.stderr}\n{child.stdout}")
            report = json.loads(child.stdout)
            violation = next((v for v in report["violations"] if v["property"] == property_name), None)
            if violation is None:
                raise RuntimeError(f"mutation missed {property_name}: {name}: {report}")
            results.append(dict(name=name, healthy="PASS", detected=True, violation=violation))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--depth", type=int, default=4)
    parser.add_argument("--seconds", type=int, default=3000)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--witness", choices=WITNESSES)
    group.add_argument("--bounded-only", action="store_true")
    group.add_argument("--sustained-only", action="store_true")
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if not 0 <= args.depth <= 4 or not 10 <= args.seconds <= 3000 or args.seconds % 10:
        parser.error("depth must be 0..4; seconds must be a multiple of 10 in 10..3000")
    sys.path.insert(0, str(args.source_root.resolve()))
    logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(check(args.depth, args.witness, args.seconds,
                                   args.bounded_only, args.sustained_only))
        if args.mutation_check and report["status"] == "PASS":
            report["mutations"] = mutation_check(args.source_root.resolve())
            restored = asyncio.run(check(args.depth, args.witness, args.seconds,
                                         args.bounded_only, args.sustained_only))
            report["restored"] = restored["status"]
            report["restored_matches"] = restored == {k: v for k, v in report.items()
                                                      if k not in ("mutations", "restored")}
            if not report["restored_matches"]:
                raise RuntimeError("restored result differs from the baseline")
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
