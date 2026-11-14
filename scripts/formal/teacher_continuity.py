"""Check morph caption delivery across teacher disconnects and rejoins."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from itertools import product
import json
import logging
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

from lesson_endurance import Clock, clock_scope
from lesson_liveness import Socket, Violation, callbacks
from morph_lesson import Fragment, LessonCase

ROOT = Path(__file__).resolve().parents[2]
SYNC_COMMIT = "7c6d2ba2deb734f69e15661b4d61fd4e35867380"
JOIN_CODE = "4831"
TEACHER_CODE = "604831"
OPERATIONS = ("clause", "disconnect", "rejoin", "pause", "start", "asr", "mt", "paused_pcm")
WITNESSES = ("asr_pending", "vad_open", "morph_pending", "mt_full", "manual_pause", "takeover")
SILENCE = bytes(3200)


@dataclass
class Expected:
    generation: int = 0
    owner: int | None = None
    state: str = "idle"
    auto_paused: bool = False
    accepted: int = 0
    pending: list[str] = field(default_factory=list)
    turns: list[str] = field(default_factory=list)

    def join(self):
        self.generation += 1
        self.owner = self.generation
        if self.auto_paused:
            self.state = "live"
            self.auto_paused = False

    def disconnect(self):
        self.owner = None
        if self.state == "live":
            self.auto_paused = True
            self.state = "paused"
            self.boundary()

    def control(self, action):
        self.auto_paused = False
        self.state = "live" if action == "start" else "paused"
        if action == "pause":
            self.boundary()

    def audio(self, fragment):
        self.accepted += 1
        self.pending.append(fragment.text)
        if fragment.ends_turn:
            self.boundary()

    def boundary(self):
        if self.pending:
            self.turns.append("".join(self.pending))
            self.pending.clear()


class Wire(Socket):
    def __init__(self, lesson, limits, generation=None):
        from server.connection import Connection

        super().__init__()
        self.generation = generation
        self.connection = Connection(self, "127.0.0.1", lesson, limits)
        self.handling = self.connection.handling()
        self.closing = asyncio.Event()
        self.close_allowed = asyncio.Event()
        self.close_allowed.set()

    async def close(self, code=1000):
        self.closing.set()
        await self.close_allowed.wait()
        await super().close(code)

    async def join(self, *, lang=None):
        await self.handling.__aenter__()
        await self.connection.receive(json.dumps({
            "type": "join", "role": "student" if lang else "teacher",
            "code": JOIN_CODE if lang else TEACHER_CODE, "lang": lang,
        }))

    async def disconnect(self):
        await self.handling.__aexit__(None, None, None)


class Classroom(LessonCase):
    def __init__(self, **settings):
        from server.lesson import Lesson

        super().__init__(**settings)
        self.lesson = Lesson(self.pipeline, self.config, join_code=JOIN_CODE, teacher_code=TEACHER_CODE)
        self.ledger = Expected()
        self.teacher = None
        self.students = {lang: Wire(self.lesson, self.config.limits) for lang in ("en", "zh")}
        self.open_wires = set()

    async def start(self):
        await self.pipeline.start()
        await callbacks()
        if not self.pipeline.ready:
            raise RuntimeError("warmup did not settle")
        for lang, wire in self.students.items():
            self.open_wires.add(wire)
            await wire.join(lang=lang)
        await self.join()
        await self.control("start")

    async def close(self):
        try:
            await self.lesson.shutdown()
            for wire in self.open_wires:
                await wire.disconnect()
            self.open_wires.clear()
        finally:
            await self.pipeline.stop()
            await callbacks()

    def check_state(self):
        owners = [client.ws.generation for client in self.roster.clients if client.role == "teacher"]
        expected = [] if self.ledger.owner is None else [self.ledger.owner]
        self.require(owners == expected, "teacher_owner", expected, owners)
        self.require(self.lesson.state == self.ledger.state and self.lesson.auto_paused == self.ledger.auto_paused,
                     "lesson_state", [self.ledger.state, self.ledger.auto_paused],
                     [self.lesson.state, self.lesson.auto_paused])
        for student in self.students.values():
            if self.ledger.state != "idle":
                state = next((f["state"] for f in reversed(student.frames) if f["type"] == "session"), None)
                self.require(state == self.ledger.state, "student_state", self.ledger.state, state)

    async def join(self):
        self.trace.append("join teacher")
        self.ledger.join()
        self.teacher = Wire(self.lesson, self.config.limits, self.ledger.owner)
        self.open_wires.add(self.teacher)
        await self.teacher.join()
        await callbacks()
        self.check_state()

    async def disconnect(self):
        self.trace.append("disconnect teacher")
        self.ledger.disconnect()
        wire, self.teacher = self.teacher, None
        await wire.disconnect()
        self.open_wires.remove(wire)
        if wire.closed == 1011:
            raise RuntimeError("Connection.handling aborted during teacher disconnect")
        await callbacks()
        self.check_state()

    async def control(self, action):
        self.trace.append(action)
        self.ledger.control(action)
        await self.teacher.connection.receive(json.dumps({"type": "control", "action": action}))
        await callbacks()
        self.check_state()

    def plan_audio(self, kind, *, admitted=True):
        identity = len(self.fragments) + 1
        words = "温度が高いので" if kind == "clause" else "体積が増えます。"
        fragment = Fragment(identity, f"【{identity}】{words}", kind == "sentence")
        self.fragments[identity] = fragment
        if admitted:
            self.ledger.audio(fragment)
        sample = (1000 + identity).to_bytes(2, "little", signed=True)
        return sample * 1600

    async def pcm_frames(self, data, count, *, wire=None):
        wire = self.teacher if wire is None else wire
        for _ in range(count):
            await wire.connection.receive(data)
            await callbacks()

    async def audio(self, kind, *, admitted=True, silence=5):
        self.trace.append(kind if admitted else "paused PCM")
        await self.pcm_frames(self.plan_audio(kind, admitted=admitted), 10)
        await self.pcm_frames(SILENCE, silence)

    async def step(self, operation):
        has_teacher = self.ledger.owner is not None
        live = self.ledger.state == "live"
        match operation:
            case "clause" if live:
                await self.audio("clause")
            case "disconnect" if has_teacher:
                await self.disconnect()
            case "rejoin" if not has_teacher:
                await self.join()
            case "pause" if live:
                await self.control("pause")
            case "start" if has_teacher and not live:
                await self.control("start")
            case "paused_pcm" if has_teacher and not live:
                await self.audio("sentence", admitted=False)
            case "asr" | "mt":
                executor = self.asr if operation == "asr" else self.mt
                if executor.current is None:
                    return False
                self.trace.append(f"complete {operation.upper()}")
                executor.complete()
                await callbacks()
            case _:
                return False
        self.workers_alive()
        return True

    def compare(self):
        for lang, socket in self.students.items():
            wanted = [(seq, lang, text, f"{lang}:{text}")
                      for seq, text in enumerate(self.ledger.turns, 1)]
            actual = [(f["seq"], f["lang"], f["ja"], f["text"])
                      for f in socket.frames if f["type"] == "caption"]
            self.compare_sequence("bilingual_delivery", wanted, actual)
            self.require(socket.closed is None, "bilingual_delivery", "connection open", socket.closed)
        self.require(self.pipeline.asr_dropped_segments == 0 and self.pipeline.audio_frames_rejected == 0,
                     "audio_admission", [0, 0],
                     [self.pipeline.asr_dropped_segments, self.pipeline.audio_frames_rejected])

    async def next_sentence(self):
        if self.ledger.owner is None:
            await self.join()
        if self.ledger.state == "paused":
            await self.control("start")
        if self.ledger.pending:
            self.trace.append("natural boundary")
            self.ledger.boundary()
            await self.pcm_frames(SILENCE, 5)
            await self.settle()
        await self.audio("sentence")
        await self.settle()
        self.require(self.pipeline.asr_calls == self.ledger.accepted, "audio_admission",
                     self.ledger.accepted, self.pipeline.asr_calls)


@asynccontextmanager
async def classroom(**settings):
    case = Classroom(**settings)
    try:
        await case.start()
        yield case
    finally:
        await case.close()


async def schedule(operations):
    async with classroom() as case:
        await case.audio("clause")
        case.require(case.asr.current is not None, "asr_pending_reached", "active ASR", "missing")
        for operation in operations:
            if not await case.step(operation):
                return False
        await case.settle()
        await case.next_sentence()
        return True


async def probe(case, name):
    if name == "takeover":
        await takeover(case)
        return
    if name == "mt_full":
        await case.audio("sentence")
        await case.step("asr")
        case.require(case.mt.current is not None
                     and any(f.get("code") == "mt_backlog" for f in case.teacher.frames),
                     "backpressure_reached", "active MT and queue-full warning", "missing")
    await case.audio("clause", silence=0 if name == "vad_open" else 5)
    if name == "vad_open":
        case.require(case.pipeline._segmenter.is_open and case.asr.current is None,
                     "vad_open_reached", "open audio with no ASR", "missing")
    if name == "morph_pending":
        await case.step("asr")
        case.require(case.pipeline._assembler.has_pending and case.asr.current is None,
                     "morph_pending_reached", "open Turn with no ASR", "missing")
    if name == "asr_pending":
        case.require(case.asr.current is not None, "asr_pending_reached", "active ASR", "missing")
    if name == "manual_pause":
        await case.control("pause")
    await case.disconnect()
    if name != "mt_full":
        await case.settle()
    await case.join()
    if name == "mt_full":
        await case.audio("sentence")
        await case.settle()
    if name == "manual_pause":
        await case.audio("sentence", admitted=False)
        await case.settle()


async def takeover(case):
    await case.audio("clause")
    await case.step("asr")
    old = case.teacher
    old.close_allowed.clear()
    joining = asyncio.create_task(case.join())
    try:
        await asyncio.wait_for(old.closing.wait(), timeout=2)
        case.trace.append("old PCM while replacement awaits socket close")
        await old.connection.receive(case.plan_audio("sentence", admitted=False))
    finally:
        old.close_allowed.set()
        await joining
    await old.disconnect()
    case.open_wires.remove(old)
    case.check_state()
    await case.settle()
    case.ledger.boundary()
    case.trace.append("natural boundary")
    await case.pcm_frames(SILENCE, 5)
    await case.settle()


async def witness(name):
    async with classroom() as case:
        await probe(case, name)
        await case.next_sentence()
        return dict(status="PASS", accepted=case.ledger.accepted, turns=len(case.ledger.turns))


async def sustained(seconds):
    clock = Clock()
    deadlines = deque()
    baseline_tasks = asyncio.all_tasks()
    asr_ticks, mt_ticks = 8, 8
    bounds = dict(audio_seconds=30, asr_events=8, mt_jobs=1, pending_parts=12,
                  history=50, cache=512, receipts=100, tasks=len(baseline_tasks) + 10)
    peaks = dict.fromkeys(bounds, 0)
    pcm_frames = 0
    sent = dict.fromkeys(("en", "zh"), 0)
    counted_frames = dict.fromkeys(sent, 0)
    case = Classroom(clock=clock, asr_ticks=asr_ticks, mt_ticks=mt_ticks)
    try:
        with clock_scope(clock):
            await case.start()
            voice = SILENCE
            starts = {0: "clause", 30: "clause", 45: "sentence", 70: "clause",
                      91: "sentence", 120: "clause", 150: "sentence"}
            for tick in range(seconds * 10 + 100):
                clock.tick = tick
                phase = tick % 200
                case.trace = [f"virtual PCM {tick / 10:.1f}s (cycle phase {phase / 10:.1f}s)"]
                case.asr.release_due()
                case.mt.release_due()
                await callbacks()
                if tick < seconds * 10:
                    if phase in (15, 85):
                        if phase == 15:
                            case.require(case.asr.current is not None, "asr_pending_reached",
                                         "active during teacher disconnect", "missing")
                        await case.disconnect()
                    if phase in (20, 90):
                        await case.join()
                    if phase == 80:
                        await case.control("pause")
                    if phase == 110:
                        await case.control("start")
                    if phase in starts:
                        voice = case.plan_audio(starts[phase], admitted=phase != 91)
                    if phase not in range(15, 20) and phase not in range(85, 90):
                        speaking = any(start <= phase < start + 10 for start in starts)
                        await case.pcm_frames(voice if speaking else SILENCE, 1)
                        pcm_frames += 1
                    if phase == 139:
                        case.ledger.boundary()
                    if phase in (15, 59, 80, 139, 164):
                        deadlines.append((tick + 80, len(case.ledger.turns)))
                case.workers_alive()
                p = case.pipeline
                sizes = dict(audio_seconds=p.audio_queue_seconds, asr_events=p.asr_queue_depth,
                             mt_jobs=p.captions.queue_depth, pending_parts=len(p._assembler._pending.parts),
                             history=len(p.captions.history.since(0)), cache=p.captions.metrics().cache.size,
                             receipts=max((len(c.caption_languages) for c in case.roster.students()), default=0),
                             tasks=len(asyncio.all_tasks()))
                case.require(all(sizes[key] <= bounds[key] for key in bounds),
                             "retained_state_bound", bounds, sizes)
                for key, value in sizes.items():
                    peaks[key] = max(peaks[key], value)
                case.require(p.asr_dropped_segments == 0, "audio_admission", 0, p.asr_dropped_segments)
                while deadlines and deadlines[0][0] <= tick:
                    due, count = deadlines.popleft()
                    for lang, socket in case.students.items():
                        frames = socket.frames
                        sent[lang] += sum(f["type"] == "caption" for f in frames[counted_frames[lang]:])
                        counted_frames[lang] = len(frames)
                        case.require(sent[lang] >= count, "sustained_deadline",
                                     dict(lang=lang, due_s=due / 10, captions=count), sent[lang])
            case.compare()
            case.require(p.asr_calls == case.ledger.accepted, "audio_admission",
                         case.ledger.accepted, p.asr_calls)
            case.require(not deadlines and p.queue_depth == 0 and case.asr.current is None
                         and case.mt.current is None and not p._assembler.has_pending,
                         "drain_completion", "all input settled before teardown", "unfinished work")
    finally:
        await asyncio.wait_for(case.close(), timeout=3)
    remaining = sorted(task.get_name() for task in asyncio.all_tasks() - baseline_tasks)
    case.require(not remaining and case.asr.closed and case.mt.closed and not case.roster.clients
                 and p.queue_depth == 0, "stop_completion", "no tasks, clients or queued work",
                 dict(tasks=remaining, executors_closed=[case.asr.closed, case.mt.closed],
                      clients=len(case.roster.clients), queue_depth=p.queue_depth))
    return dict(status="PASS", seconds=seconds, pcm_frames=pcm_frames, asr_service_s=asr_ticks / 10,
                mt_service_s_per_language=mt_ticks / 10, teacher_rejoins=case.ledger.generation - 1,
                accepted=case.ledger.accepted, captions=sent, bounds=bounds, peaks=peaks)


async def check(depth=4, only_witness=None, seconds=3000, bounded_only=False, only_sustained=False):
    attempted = passed = illegal = failed = 0
    violations = {}
    witnesses = {}
    lesson = None
    if not only_witness and not only_sustained:
        for size in range(depth + 1):
            for operations in product(OPERATIONS, repeat=size):
                attempted += 1
                try:
                    if await schedule(operations):
                        passed += 1
                    else:
                        illegal += 1
                except Violation as error:
                    failed += 1
                    violations.setdefault(error.detail["property"], error.detail)
    for name in ([only_witness] if only_witness else () if only_sustained else WITNESSES):
        try:
            witnesses[name] = await witness(name)
        except Violation as error:
            witnesses[name] = dict(status="FAIL")
            violations.setdefault(error.detail["property"], dict(witness=name, **error.detail))
    if not only_witness and not bounded_only:
        try:
            lesson = await sustained(seconds)
        except Violation as error:
            lesson = dict(status="FAIL", seconds=seconds)
            violations.setdefault(error.detail["property"], error.detail)
    return dict(status="FAIL" if violations else "PASS", sync_commit=SYNC_COMMIT,
                python=platform.python_version(), depth=depth, attempted=attempted,
                legal_passed=passed, illegal=illegal, failed=failed, witnesses=witnesses,
                sustained=lesson, violations=list(violations.values()))


@dataclass(frozen=True)
class Mutation:
    name: str
    property_name: str
    probe: str
    path: str
    before: str
    after: str


MUTATIONS = (
    Mutation("lose_disconnect_flush", "bilingual_delivery", "morph_pending", "server/lesson.py",
             '            self.auto_paused = True\n            await self._transition("paused")',
             '            self.auto_paused = True\n            self.state = "paused"\n'
             '            await self._transition("paused")'),
    Mutation("flush_before_asr", "bilingual_delivery", "asr_pending", "server/pipeline.py",
             "        await self._enqueue_asr(FlushTurn(closed_at=time.monotonic()))",
             "        await self._close_pending_turn(FlushTurn(closed_at=time.monotonic()))"),
    Mutation("lose_open_audio", "bilingual_delivery", "vad_open", "server/pipeline.py",
             "        if segment is not None:\n            await self._enqueue_asr(segment)",
             "        if segment is not None:\n            pass"),
    Mutation("never_resume", "lesson_state", "asr_pending", "server/lesson.py",
             "            if self.auto_paused:", "            if False:"),
    Mutation("resume_manual_pause", "lesson_state", "manual_pause", "server/lesson.py",
             "            if self.auto_paused:", '            if self.state == "paused":'),
    Mutation("accept_paused_pcm", "bilingual_delivery", "manual_pause", "server/pipeline.py",
             "        if not self._accepting_audio:\n            return  # 一時停止・終了中の音声は破棄",
             "        if not self._accepting_audio:\n            pass"),
    Mutation("accept_replaced_pcm", "bilingual_delivery", "takeover", "server/connection.py",
             "            elif self._is_teacher(self._current_client()):",
             "            elif self._is_teacher(self._client):"),
    Mutation("drop_full_mt", "bilingual_delivery", "mt_full", "server/caption_delivery.py",
             "                await self._mt_queue.put(entry)", "                return False"),
    Mutation("late_boundary", "sustained_deadline", "sustained", "server/audio/vad.py",
             "if self._post_silence_run < self._force_silence_frames:",
             "if self._post_silence_run < self._force_silence_frames + 100:"),
    Mutation("unbounded_history", "retained_state_bound", "sustained", "server/caption_history.py",
             "deque(maxlen=maxlen)", "deque()"),
    Mutation("drop_accepted_audio", "audio_admission", "sustained", "server/asr_backlog.py",
             "        total_samples = self._waiting_samples + self._active_samples",
             "        if samples:\n            self.dropped_segments += 1\n            return Dropped\n"
             "        total_samples = self._waiting_samples + self._active_samples"),
    Mutation("skip_pipeline_stop", "stop_completion", "sustained", "server/pipeline.py",
             "        await finish_cleanup(self._stop_task)",
             "        self._stop_task.cancel()"),
)


def mutation_check(source_root):
    results = []
    for mutation in MUTATIONS:
        sustained_probe = mutation.probe == "sustained"
        options = (dict(only_sustained=True, seconds=240) if sustained_probe
                   else dict(only_witness=mutation.probe))
        healthy = asyncio.run(check(**options))
        if healthy["status"] != "PASS":
            raise RuntimeError(f"mutation control is not healthy: {mutation.name}: {healthy}")
        with tempfile.TemporaryDirectory(prefix="teacher-continuity-") as directory:
            scratch = Path(directory)
            shutil.copytree(source_root / "server", scratch / "server",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = scratch / mutation.path
            source = target.read_text(encoding="utf-8")
            if source.count(mutation.before) != 1:
                raise RuntimeError(f"mutation site changed: {mutation.name}")
            target.write_text(source.replace(mutation.before, mutation.after), encoding="utf-8")
            args = ["--sustained-only", "--seconds", "240"] if sustained_probe else ["--witness", mutation.probe]
            child = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                                    "--source-root", str(scratch), *args], cwd=scratch,
                                   capture_output=True, encoding="utf-8", timeout=90)
            if child.returncode != 1:
                raise RuntimeError(f"expected a violation for {mutation.name}:\n{child.stderr}\n{child.stdout}")
            report = json.loads(child.stdout)
            violation = next((v for v in report["violations"] if v["property"] == mutation.property_name), None)
            if report["status"] != "FAIL" or violation is None:
                raise RuntimeError(f"mutation missed {mutation.property_name}: {mutation.name}: {report}")
        results.append(dict(name=mutation.name, healthy="PASS", detected=True, violation=violation))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--depth", type=int, default=4)
    parser.add_argument("--seconds", type=int, default=3000)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--witness", choices=WITNESSES)
    group.add_argument("--bounded-only", action="store_true")
    group.add_argument("--sustained-only", action="store_true")
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if not 0 <= args.depth <= 4 or not 20 <= args.seconds <= 3000 or args.seconds % 20:
        parser.error("depth must be 0..4; seconds must be a multiple of 20 in 20..3000")
    sys.path.insert(0, str(args.source_root.resolve()))
    logging.disable(logging.CRITICAL)

    def run():
        return asyncio.run(check(args.depth, args.witness, args.seconds, args.bounded_only, args.sustained_only))

    try:
        report = run()
        if args.mutation_check and report["status"] == "PASS":
            mutations = mutation_check(args.source_root.resolve())
            restored = run()
            if restored != report:
                raise RuntimeError(f"restored result differs from baseline: {restored}")
            report.update(mutations=mutations, restored=restored["status"], restored_matches=True)
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
