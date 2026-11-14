"""Bounded student recovery schedules and sustained morph PCM delivery."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
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
SYNC_COMMIT = "b86592318a919b0d1b934a56002e3fd457b8b0f7"
sys.path.insert(0, str(ROOT / "scripts/formal"))
from lesson_endurance import Clock, TimedExecutor, clock_scope
from lesson_liveness import ControlledExecutor, Socket, Translation, Violation, callbacks
from morph_lesson import LessonCase, Recognition

OPERATIONS = ("sentence", "asr", "mt", "disconnect", "rejoin", "set_en", "set_zh")
WITNESSES = ("priority", "backpressure", "refresh", "recovery")


class StudentSocket(Socket):
    def __init__(self, lang):
        super().__init__()
        self.selected = lang


class Classroom(LessonCase):
    def __init__(self, *, capacity=4, watermark=3, clock=None):
        from server.config import AppConfig
        from server.inference import InferenceRunner
        from server.lesson import Lesson
        from server.pipeline import Pipeline
        from server.roster import Roster

        self.config = AppConfig.model_validate({
            "vad": {"engine": "energy", "threshold": 300},
            "turn": {"strategy": "morph", "classifier": "surface"},
            "recording": {"default_on": False},
            "limits": {"mt_queue_max": capacity, "mt_replay_watermark": watermark},
        })
        self.fragments, self.events, self.failed, self.trace = {}, [], set(), []
        self.clock = clock
        self.asr = ControlledExecutor() if clock is None else TimedExecutor(clock, 8)
        self.mt = ControlledExecutor() if clock is None else TimedExecutor(clock, 8)
        self.roster = Roster(self.config.limits)
        self.wrong_language = []
        send = self.roster.send

        def observe_send(client, payload, **options):
            accepted = send(client, payload, **options)
            if accepted and payload["type"] == "caption" and payload["lang"] != client.ws.selected:
                self.wrong_language.append((client.ws.selected, payload))
            return accepted

        self.roster.send = observe_send
        self.pipeline = Pipeline(self.roster, self.config, Recognition(self.fragments), Translation(),
                                 asr_runner=InferenceRunner(self.asr), mt_runner=InferenceRunner(self.mt))
        self.lesson = Lesson(self.pipeline, self.config, join_code="4831", teacher_code="604831")
        self.teacher = Socket()
        self.students = {}
        self.visitor = None
        self.visitor_socket = None
        self.offset = 0
        self.rejoins = 0
        self.switches = 0

    async def start(self):
        await self.roster.add(self.teacher, "teacher")
        await self.pipeline.start()
        await callbacks()
        if not self.pipeline.ready:
            raise RuntimeError("warmup did not settle")
        await self.lesson.control("start")

    async def join_student(self, socket, lang, last_seq):
        from server.ws_protocol import JoinMessage

        return await self.lesson.join(socket, None, JoinMessage(
            type="join", role="student", code="4831", lang=lang, last_seq=last_seq,
        ), "127.0.0.1")

    async def observe(self):
        self.offset = len(self.expected())
        for lang in ("en", "zh"):
            socket = StudentSocket(lang)
            self.students[lang] = socket
            await self.join_student(socket, lang, self.offset)
        await callbacks()

    def connected(self):
        return self.visitor is not None and self.roster.contains(self.visitor)

    async def rejoin(self, last_seq=0, lang="en"):
        if self.connected():
            return False
        self.trace.append(f"rejoin({lang}, last_seq={last_seq})")
        self.visitor_socket = StudentSocket(lang)
        self.visitor = await self.join_student(self.visitor_socket, lang, last_seq)
        self.rejoins += 1
        await callbacks()
        return True

    async def disconnect(self):
        if not self.connected():
            return False
        self.trace.append("disconnect visitor")
        await self.lesson.leave(self.visitor)
        self.visitor = None
        await callbacks()
        return True

    async def set_language(self, lang):
        if not self.connected():
            return False
        self.trace.append(f"set_language({lang})")
        self.visitor_socket.selected = lang
        await self.lesson.set_lang(self.visitor, lang)
        self.switches += 1
        await callbacks()
        return True

    async def step(self, operation, **kwargs):
        if operation not in ("disconnect", "rejoin", "set_en", "set_zh"):
            return await super().step(operation, **kwargs)
        if operation == "disconnect":
            legal = await self.disconnect()
        elif operation == "rejoin":
            legal = await self.rejoin()
        else:
            legal = await self.set_language(operation[4:])
        self.workers_alive()
        return legal

    def compare(self):
        expected = list(enumerate(self.expected(), 1))
        actual = [(f["seq"], f["ja"]) for f in self.teacher.frames if f["type"] == "asr_final"]
        self.compare_sequence("morph_turns", expected, actual)
        for lang, socket in self.students.items():
            wanted = [(seq, text, f"{lang}:{text}", lang) for seq, text in expected if seq > self.offset]
            actual = [(f["seq"], f["ja"], f["text"], f["lang"])
                      for f in socket.frames if f["type"] == "caption"]
            self.compare_sequence("bilingual_delivery", wanted, actual)
            self.require(socket.closed is None, "bilingual_delivery", "connection open", socket.closed)
        self.require(not self.wrong_language, "selected_language", [], self.wrong_language[:1])

    async def seed(self, count=3):
        for _ in range(count):
            await self.step("sentence")
            await self.step("asr")
        await self.settle()
        await self.observe()

    async def close(self):
        tasks = [*self.pipeline._tasks, self.pipeline._warmup_task,
                 self.pipeline.captions._worker_task,
                 *(sender._task for sender in self.roster._senders.values()),
                 *(sender._close_task for sender in self.roster._senders.values())]
        await self.pipeline.stop()
        await callbacks()
        self.require(not self.pipeline._tasks and self.pipeline.captions._worker_task is None
                     and not self.roster.clients and self.asr.closed and self.mt.closed
                     and all(task.done() for task in tasks if task is not None),
                     "stop_completion", "workers, senders and executors stopped", "resources remain")


async def schedule(operations):
    case = Classroom()
    try:
        await case.start()
        await case.seed()
        await case.rejoin()
        await case.step("sentence")
        for operation in operations:
            if not await case.step(operation):
                return False
        await case.settle()
        await case.step("sentence")
        await case.settle()
        case.require(case.pipeline.asr_dropped_segments == 0,
                     "audio_admission", 0, case.pipeline.asr_dropped_segments)
        return True
    finally:
        await case.close()


async def witness(name):
    case = Classroom(capacity=1, watermark=1) if name in ("backpressure", "recovery") else Classroom()
    try:
        await case.start()
        await case.seed(1 if name in ("backpressure", "recovery") else 3)
        await case.rejoin()
        case.require(case.mt.current is not None, "replay_reached", "held history translation", None)
        if name == "refresh":
            await case.settle()
            await case.set_language("zh")
            expected = (case.expected()[0], "zh")
            actual = None if case.mt.current is None else case.mt.current.key
            case.require(actual == expected and case.pipeline.captions.queue_depth == 0
                         and bool(case.pipeline.captions._history_refreshes),
                         "refresh_reached", expected, actual)
            await case.set_language("en")
        await case.step("sentence")
        await case.step("asr")
        if name == "priority":
            case.require(case.pipeline.captions.queue_depth == 4,
                         "priority_reached", "two replay and two live jobs queued", case.pipeline.captions.queue_depth)
            await case.step("mt")
            wanted = (case.expected()[-1], "en")
            actual = None if case.mt.current is None else case.mt.current.key
            case.require(actual == wanted, "live_priority", wanted, actual)
        elif name in ("backpressure", "recovery"):
            case.require(case.pipeline.captions.queue_depth == 1 and
                         any(f.get("code") == "mt_backlog" for f in case.teacher.frames),
                         "backpressure_reached", "live enqueue waits behind full queue", case.pipeline.queue_depth)
            await case.disconnect()
            await case.rejoin()
            case.require(case.visitor_socket.closed == 1013 and not case.connected(),
                         "recovery_retry", "1013 with detached visitor", case.visitor_socket.closed)
        await case.settle()
        if name == "recovery":
            await case.rejoin(last_seq=len(case.expected()) - 1)
            await case.settle()
            captions = [f["seq"] for f in case.visitor_socket.frames if f["type"] == "caption"]
            case.require(captions == [len(case.expected())] and case.connected(),
                         "recovery_delivery", [len(case.expected())], captions)
        await case.step("sentence")
        await case.settle()
        return dict(status="PASS", **case.summary())
    finally:
        await case.close()


async def sustained(seconds):
    import numpy as np

    clock = Clock()
    case = Classroom(clock=clock)
    deadlines = deque()
    peaks = dict(audio_seconds=0, asr_events=0, mt_jobs=0, history=0, cache=0,
                 receipts=0, refreshes=0, refresh_entries=0, students=0, send_queue=0, tasks=0)
    voice = bytes(3200)
    with clock_scope(clock):
        try:
            await case.start()
            await case.seed()
            task_bound = len(asyncio.all_tasks()) + 4
            for tick in range(seconds * 10 + 100):
                clock.tick = tick
                case.trace = [f"PCM {min(tick, seconds * 10) / 10}s", "ASR and MT service 0.8s"]
                case.asr.release_due()
                case.mt.release_due()
                await callbacks()
                if tick < seconds * 10:
                    phase = tick % 100
                    if phase == 10:
                        await case.rejoin(last_seq=max(0, case.pipeline.captions.history.seq_head - 2))
                    elif phase in (32, 62):
                        await case.set_language("zh" if phase == 32 else "en")
                    elif phase == 90:
                        await case.disconnect()
                    kinds = {0: "clause", 15: "sentence", 50: "clause"}
                    if phase in kinds:
                        identity = case.fragment(kinds[phase])
                        voice = np.full(1600, 1000 + identity, dtype="<i2").tobytes()
                    speaking = phase < 10 or 15 <= phase < 25 or 50 <= phase < 60
                    await case.pipeline.feed_audio(voice if speaking else bytes(3200))
                    await callbacks()
                    if phase == 69:
                        case.events.append("boundary")
                    if phase in (29, 69):
                        deadlines.append((tick + 80, len(case.expected()) - case.offset))
                case.workers_alive()
                p = case.pipeline
                sizes = dict(audio_seconds=p.audio_queue_seconds, asr_events=p.asr_queue_depth,
                             mt_jobs=p.captions.queue_depth, history=len(p.captions.history.since(0)),
                             cache=p.captions.metrics().cache.size,
                             receipts=max((len(c.caption_languages) for c in case.roster.students()), default=0),
                             refreshes=len(p.captions._history_refreshes),
                             refresh_entries=sum(len(r.utterances) for r in p.captions._history_refreshes.values()),
                             students=len(case.roster.students()),
                             send_queue=max((s.pending for s in case.roster._senders.values()), default=0),
                             tasks=len(asyncio.all_tasks()))
                bounds = dict(audio_seconds=30, asr_events=8, mt_jobs=4, history=50, cache=512,
                              receipts=100, refreshes=1, refresh_entries=50, students=3,
                              send_queue=case.config.limits.send_queue_max, tasks=task_bound)
                case.require(all(sizes[k] <= bounds[k] for k in sizes), "retained_state_bound", bounds, sizes)
                for key, value in sizes.items():
                    peaks[key] = max(peaks[key], value)
                case.require(p.asr_dropped_segments == 0, "audio_admission", 0, p.asr_dropped_segments)
                while deadlines and deadlines[0][0] <= tick:
                    _, count = deadlines.popleft()
                    for lang, socket in case.students.items():
                        delivered = sum(f["type"] == "caption" for f in socket.frames)
                        case.require(delivered >= count, "sustained_deadline",
                                     dict(lang=lang, at_least=count), delivered)
            case.compare()
            case.require(not deadlines and case.pipeline.queue_depth == 0 and case.asr.current is None
                         and case.mt.current is None and not case.pipeline._assembler.has_pending,
                         "drain_completion", "input settled without a final flush", "unfinished work")
            stats = sum(f["type"] == "stats" for f in case.teacher.frames)
            case.require(stats >= seconds // 2, "stats_continuation", seconds // 2, stats)
            return dict(status="PASS", seconds=seconds, pcm_frames=seconds * 10,
                        prelude_turns=case.offset, rejoins=case.rejoins, language_changes=case.switches,
                        stats_frames=stats, peaks=peaks, **case.summary())
        finally:
            await case.close()


async def check(depth=4, only_witness=None, seconds=3000, bounded_only=False, only_sustained=False):
    attempted = legal = failed = 0
    violations, witnesses, lessons = {}, {}, []

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
        try:
            lessons.append(await sustained(seconds))
        except Violation as error:
            record(error)
    return dict(status="FAIL" if violations else "PASS", sync_commit=SYNC_COMMIT,
                python=platform.python_version(), depth=depth, attempted=attempted,
                legal_passed=legal, illegal=attempted - legal - failed, failed=failed,
                witnesses=witnesses, sustained=lessons, violations=list(violations.values()))


MUTATIONS = (
    ("replay_before_live", "live_priority", "priority", "server/caption_delivery.py",
     "_LIVE_PRIORITY = 0", "_LIVE_PRIORITY = 2"),
    ("drop_full_live", "bilingual_delivery", "backpressure", "server/caption_delivery.py",
     "                await self._mt_queue.put(entry)", "                return False"),
    ("skip_mt_drain", "drain_completion", "priority", "server/pipeline.py",
     "        await self.captions.drain(timeout)", "        pass"),
    ("silent_replay_loss", "recovery_retry", "recovery", "server/caption_delivery.py",
     "                await self.roster.retry(client)", "                return"),
    ("omit_recovery", "recovery_delivery", "recovery", "server/caption_delivery.py",
     "        for utterance in self.history.since(last_seq):",
     "        for utterance in ([] if last_seq else self.history.since(last_seq)):"),
    ("stale_refresh", "selected_language", "refresh", "server/caption_delivery.py",
     "            if self._history_refreshes.get(client_id) is not refresh:\n                return",
     "            if False:\n                return"),
    ("corrupt_final_text", "morph_turns", "backpressure", "server/caption_history.py",
     "Utterance(self._seq, t_start, t_end, text_ja, asr_ms, segments=segments)",
     "Utterance(self._seq, t_start, t_end, 'broken', asr_ms, segments=segments)"),
    ("unbounded_history", "retained_state_bound", "sustained", "server/caption_history.py",
     "deque(maxlen=maxlen)", "deque()"),
    ("late_boundary", "sustained_deadline", "sustained", "server/audio/vad.py",
     "if self._post_silence_run < self._force_silence_frames:",
     "if self._post_silence_run < self._force_silence_frames + 100:"),
    ("reject_pcm", "audio_admission", "sustained", "server/asr_backlog.py",
     "        total_samples = self._waiting_samples + self._active_samples",
     "        if samples and event.t_start >= 5:\n            self.dropped_segments += 1\n            return Dropped\n        total_samples = self._waiting_samples + self._active_samples"),
    ("stop_stats", "worker_liveness", "sustained", "server/pipeline.py",
     "            await self.send_to_teacher(self.stats_snapshot())", "            return"),
    ("lose_stats", "stats_continuation", "sustained", "server/pipeline.py",
     "            await self.send_to_teacher(self.stats_snapshot())", "            pass"),
    ("leave_senders", "stop_completion", "priority", "server/pipeline.py",
     "asyncio.gather(*pending, return_exceptions=True), self.roster.aclose(),",
     "asyncio.gather(*pending, return_exceptions=True),"),
    ("forget_live_workers", "stop_completion", "priority", "server/pipeline.py",
     "        for task in self._tasks:\n            task.cancel()",
     "        self._tasks = []"),
)


def mutation_check(source_root):
    results = []
    for name, property_name, probe, relative, before, after in MUTATIONS:
        options = dict(only_sustained=True, seconds=300) if probe == "sustained" else dict(only_witness=probe)
        healthy = asyncio.run(check(**options))
        if healthy["status"] != "PASS":
            raise RuntimeError(f"mutation control is not healthy: {name}: {healthy}")
        with tempfile.TemporaryDirectory(prefix="student-continuity-") as directory:
            scratch = Path(directory)
            shutil.copytree(source_root / "server", scratch / "server",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = scratch / relative
            source = target.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"mutation site changed: {name}")
            target.write_text(source.replace(before, after), encoding="utf-8")
            args = ["--sustained-only", "--seconds", "300"] if probe == "sustained" else ["--witness", probe]
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
        report = asyncio.run(check(args.depth, args.witness, args.seconds, args.bounded_only, args.sustained_only))
        if args.mutation_check and report["status"] == "PASS":
            report["mutations"] = mutation_check(args.source_root.resolve())
            restored = asyncio.run(check(args.depth, args.witness, args.seconds, args.bounded_only, args.sustained_only))
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
