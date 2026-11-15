"""Check morph PCM delivery while one student transport stalls or fails."""
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
SYNC_COMMIT = "f53aaf2e30b5fab249015e52a67b38b76dd330ec"
sys.path.insert(0, str(Path(__file__).parent))
from lesson_endurance import Clock, TimedExecutor, clock_scope
from lesson_liveness import ControlledExecutor, Socket, Translation, Violation, callbacks
from morph_lesson import LessonCase, Recognition

OPERATIONS = ("sentence", "asr", "mt", "send_ok", "send_error", "send_timeout")
WITNESSES = ("overflow", "error", "timeout", "recovery")


class SlowSocket(Socket):
    def __init__(self):
        super().__init__()
        self.pending = None
        self.automatic = False
        self.close_calls = []

    async def send_json(self, payload):
        if payload["type"] == "caption" and not self.automatic:
            self.pending = asyncio.get_running_loop().create_future()
            try:
                await self.pending
            finally:
                self.pending = None
        self.frames.append(payload)

    async def close(self, code=1000):
        self.close_calls.append(code)
        await super().close(code)

    def release(self, outcome="ok"):
        if self.pending is None or self.pending.done():
            return False
        if outcome == "ok":
            self.pending.set_result(None)
        elif outcome == "error":
            self.pending.set_exception(ConnectionResetError("injected transport failure"))
        else:
            self.pending.set_exception(TimeoutError("injected timeout outcome"))
        return True


class Classroom(LessonCase):
    def __init__(self, capacity=2, *, clock=None):
        from server.config import AppConfig
        from server.inference import InferenceRunner
        from server.lesson import Lesson
        from server.pipeline import Pipeline
        from server.roster import Roster

        self.config = AppConfig.model_validate({
            "vad": {"engine": "energy", "threshold": 300},
            "turn": {"strategy": "morph", "classifier": "surface"},
            "partial": {"enabled": False},
            "recording": {"default_on": False},
            "monitoring": {"stats_interval_s": 3600},
            "limits": {"send_queue_max": capacity, "send_timeout_s": 3600,
                       "mt_queue_max": 8, "mt_replay_watermark": 8},
        })
        self.fragments, self.events, self.failed, self.trace = {}, [], set(), []
        self.clock = clock
        self.asr = ControlledExecutor() if clock is None else TimedExecutor(clock, 8)
        self.mt = ControlledExecutor() if clock is None else TimedExecutor(clock, 8)
        self.roster = Roster(self.config.limits)
        self.pipeline = Pipeline(self.roster, self.config, Recognition(self.fragments), Translation(),
                                 asr_runner=InferenceRunner(self.asr), mt_runner=InferenceRunner(self.mt))
        self.lesson = Lesson(self.pipeline, self.config, join_code="4831", teacher_code="604831")
        self.teacher = Socket()
        self.students = {lang: Socket() for lang in ("en", "zh")}
        self.slow = None
        self.client = None
        self.sender = None
        self.retired_tasks = []
        self.faults = dict(overflow=0, error=0, timeout=0)

    async def add_slow(self, timeout=3600):
        self.slow = SlowSocket()
        self.config.limits.send_timeout_s = timeout
        self.client = await self.roster.add(self.slow, "student", "en")
        self.sender = self.roster._senders[self.client.id]
        self.config.limits.send_timeout_s = 3600
        await callbacks()

    def transport_bounds(self):
        sizes = [s.pending for s in self.roster._senders.values()]
        self.require(all(size <= self.config.limits.send_queue_max for size in sizes),
                     "send_queue_bound", self.config.limits.send_queue_max, sizes)
        self.require(all(s.closed is None for s in self.students.values()) and self.teacher.closed is None,
                     "slow_isolation", "healthy teacher and English/Chinese students stay open",
                     [self.teacher.closed, *(s.closed for s in self.students.values())])

    async def settle(self):
        drain = asyncio.create_task(self.pipeline.drain(timeout=None))
        try:
            for _ in range(6 * (len(self.fragments) + 2)):
                await callbacks()
                self.workers_alive()
                self.transport_bounds()
                if drain.done():
                    await drain
                    await callbacks()
                    self.require(self.pipeline.queue_depth == 0 and self.asr.current is None
                                 and self.mt.current is None, "drain_completion", "no inference remains", "pending")
                    self.compare()
                    return
                for executor in (self.asr, self.mt):
                    if executor.current is not None:
                        executor.complete()
            self.require(False, "slow_isolation", "inference drains while slow send stays blocked", "pending")
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)

    async def remove_slow(self):
        if self.client is None:
            return
        self.slow.automatic = True
        self.slow.release()
        await callbacks()
        self.retired_tasks.extend(t for t in (self.sender._task, self.sender._close_task) if t is not None)
        await self.lesson.leave(self.client)
        self.client = None
        await callbacks()

    async def close(self):
        await self.remove_slow()
        tasks = [*self.retired_tasks, *self.pipeline._tasks, self.pipeline._warmup_task,
                 self.pipeline.captions._worker_task,
                 *(s._task for s in self.roster._senders.values())]
        await self.pipeline.stop()
        await callbacks()
        self.require(all(t.done() for t in tasks if t is not None) and not self.roster.clients
                     and self.asr.closed and self.mt.closed,
                     "stop_completion", "all workers, senders and executors stopped", "resources remain")

    async def step(self, operation, **kwargs):
        if operation.startswith("send_"):
            if not self.sender.alive or not self.slow.release(operation[5:]):
                return False
            self.trace.append(operation)
            await callbacks()
            if operation != "send_ok":
                self.require(not self.sender.alive and self.slow.close_calls == [1013],
                             "fault_disconnect", "one 1013 close after transport failure",
                             dict(alive=self.sender.alive, closes=self.slow.close_calls))
            self.workers_alive()
        else:
            if not await super().step(operation, **kwargs):
                return False
        self.transport_bounds()
        return True

    async def seed(self):
        await self.start()
        await self.add_slow()
        await self.step("sentence")
        await self.settle()
        self.require(self.slow.pending is not None, "slow_send_reached", "held caption send", None)


async def schedule(operations, capacity):
    case = Classroom(capacity)
    try:
        await case.seed()
        case.trace = [f"send capacity={capacity}", "one PCM caption send held"]
        for operation in operations:
            if not await case.step(operation):
                return False
        await case.settle()
        await case.step("sentence")
        await case.settle()
        case.require(case.pipeline.asr_dropped_segments == 0,
                     "audio_admission", 0, case.pipeline.asr_dropped_segments)
        return {operation: operations.count(operation) for operation in OPERATIONS}
    finally:
        await case.close()


async def witness(name):
    case = Classroom(8 if name == "recovery" else 2)
    clock = Clock()
    with clock_scope(clock):
        try:
            await case.seed()
            if name == "timeout":
                await case.remove_slow()
                await case.add_slow(timeout=0.5)
                await case.step("sentence")
                await case.settle()
                case.require(case.slow.pending is not None, "slow_send_reached", "held caption send", None)
                clock.tick = 4
                await callbacks()
                case.require(case.sender.alive, "positive_timeout", "still open before 0.5s", "closed")
                clock.tick = 6
                await callbacks()
            elif name in ("error", "recovery"):
                if name == "recovery":
                    await case.step("send_ok")
                    await case.step("sentence")
                    await case.settle()
                await case.step("send_error")
            else:
                for _ in range(3):
                    await case.step("sentence")
                    await case.settle()
            case.require(not case.sender.alive and case.slow.close_calls == [1013],
                         "fault_disconnect", "one 1013 close", dict(alive=case.sender.alive,
                                                                   closes=case.slow.close_calls))
            before_fault_captions = len(case.expected())
            for _ in range(2):
                await case.step("sentence")
                await case.settle()
            recovery = None
            if name == "recovery":
                from server.ws_protocol import JoinMessage

                last_seq = max((f["seq"] for f in case.slow.frames if f["type"] == "caption"), default=0)
                case.require(last_seq == 1, "recovery_reached", 1, last_seq)
                await case.remove_slow()
                recovered = Socket()
                client = await case.lesson.join(recovered, None, JoinMessage(
                    type="join", role="student", code="4831", lang="en", last_seq=last_seq), "127.0.0.1")
                await case.settle()
                wanted = [(seq, text, f"en:{text}") for seq, text in enumerate(case.expected(), 1) if seq > last_seq]
                got = [(f["seq"], f["ja"], f["text"]) for f in recovered.frames if f["type"] == "caption"]
                case.compare_sequence("recovery_delivery", wanted, got)
                case.require(recovered.closed is None and case.roster.contains(client),
                             "recovery_delivery", "reconnected student stays open", recovered.closed)
                await case.step("sentence")
                await case.settle()
                wanted = [(seq, text, f"en:{text}") for seq, text in enumerate(case.expected(), 1) if seq > last_seq]
                got = [(f["seq"], f["ja"], f["text"]) for f in recovered.frames if f["type"] == "caption"]
                case.compare_sequence("recovery_delivery", wanted, got)
                recovery = dict(last_seq=last_seq, replayed=len(wanted) - 1, subsequent_live=1)
            return dict(status="PASS", timeout_mode="positive_wait_for" if name == "timeout" else None,
                        timeout_s=0.5 if name == "timeout" else None, recovery=recovery,
                        captions_after_fault=len(case.expected()) - before_fault_captions, **case.summary())
        finally:
            await case.close()


async def sustained(seconds):
    import numpy as np

    clock = Clock()
    case = Classroom(clock=clock)
    deadlines = deque()
    peaks = dict(audio_seconds=0, asr_events=0, mt_jobs=0, history=0, cache=0,
                 receipts=0, send_queue=0, clients=0, tasks=0)
    fault = None
    voice = bytes(3200)
    with clock_scope(clock):
        try:
            await case.start()
            task_bound = len(asyncio.all_tasks()) + 5
            for tick in range(seconds * 10 + 100):
                clock.tick = tick
                case.trace = [f"PCM {min(tick, seconds * 10) / 10}s", f"slow student {fault}"]
                case.asr.release_due()
                case.mt.release_due()
                await callbacks()
                if tick < seconds * 10:
                    cycle = tick % 200
                    if cycle == 0:
                        await case.remove_slow()
                        fault = ("overflow", "error", "timeout")[(tick // 200) % 3]
                        await case.add_slow(timeout=1 if fault == "timeout" else 3600)
                    if fault == "error" and case.slow.pending is not None and case.sender.alive:
                        case.slow.release("error")
                        await callbacks()
                    if cycle == 199:
                        case.require(not case.sender.alive and case.slow.close_calls == [1013],
                                     "fault_disconnect", dict(mode=fault, closes=[1013]),
                                     dict(alive=case.sender.alive, closes=case.slow.close_calls))
                        case.faults[fault] += 1
                    phase = tick % 100
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
                        deadlines.append((tick + 80, len(case.expected())))
                case.workers_alive()
                case.transport_bounds()
                p = case.pipeline
                sizes = dict(audio_seconds=p.audio_queue_seconds, asr_events=p.asr_queue_depth,
                             mt_jobs=p.captions.queue_depth, history=len(p.captions.history.since(0)),
                             cache=p.captions.metrics().cache.size,
                             receipts=max((len(c.caption_languages) for c in case.roster.students()), default=0),
                             send_queue=max((s.pending for s in case.roster._senders.values()), default=0),
                             clients=len(case.roster.clients), tasks=len(asyncio.all_tasks()))
                bounds = dict(audio_seconds=30, asr_events=8, mt_jobs=8, history=50, cache=512,
                              receipts=100, send_queue=2, clients=4, tasks=task_bound)
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
            case.require(not deadlines and p.queue_depth == 0 and case.asr.current is None
                         and case.mt.current is None and not p._assembler.has_pending,
                         "drain_completion", "all input settled without final flush", "unfinished work")
            return dict(status="PASS", seconds=seconds, pcm_frames=seconds * 10, faults=case.faults,
                        asr_service_s=0.8, mt_service_s=0.8, peaks=peaks, **case.summary())
        finally:
            await case.close()


async def check(depth=4, only_witness=None, seconds=3000, bounded_only=False, only_sustained=False):
    attempted = legal = failed = 0
    violations, witnesses, lessons = {}, {}, []
    coverage = {operation: 0 for operation in OPERATIONS}

    def record(error):
        violations.setdefault(error.detail["property"], error.detail)

    if not only_witness and not only_sustained:
        for capacity in (2, 3):
            for size in range(depth + 1):
                for operations in itertools.product(OPERATIONS, repeat=size):
                    attempted += 1
                    try:
                        result = await schedule(operations, capacity)
                        if result is not False:
                            legal += 1
                            for operation, count in result.items():
                                coverage[operation] += count
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
                capacities=[2, 3], legal_operation_counts=coverage,
                bounded_timeout_mode="injected_TimeoutError",
                witnesses=witnesses, sustained=lessons, violations=list(violations.values()))


MUTATIONS = (
    ("await_each_socket", "slow_isolation", "error", "server/caption_delivery.py",
     "                    if self.roster.send(client, captions[selected]):",
     "                    await client.ws.send_json(captions[selected])\n                    if True:"),
    ("wrong_translation", "bilingual_delivery", "error", "server/caption_delivery.py",
     "text=translated.text,", "text='broken',"),
    ("unbounded_send_queue", "send_queue_bound", "overflow", "server/delivery.py",
     "asyncio.Queue(maxsize=maxsize)", "asyncio.Queue(maxsize=0)"),
    ("ignore_send_error", "fault_disconnect", "error", "server/delivery.py",
     "            self._kill()\n\n    def _kill", "            pass\n\n    def _kill"),
    ("ignore_send_timeout", "fault_disconnect", "timeout", "server/delivery.py",
     "            self._kill()\n        except Exception:", "            return\n        except Exception:"),
    ("premature_timeout", "positive_timeout", "timeout", "server/delivery.py",
     "self._send_timeout_s = send_timeout_s", "self._send_timeout_s = send_timeout_s / 2"),
    ("skip_mt_drain", "drain_completion", "error", "server/pipeline.py",
     "        await self.captions.drain(timeout)", "        pass"),
    ("lose_replay", "recovery_delivery", "recovery", "server/caption_delivery.py",
     "        for utterance in self.history.since(last_seq):", "        for utterance in ():"),
    ("unbounded_history", "retained_state_bound", "sustained", "server/caption_history.py",
     "deque(maxlen=maxlen)", "deque()"),
    ("late_boundary", "sustained_deadline", "sustained", "server/audio/vad.py",
     "if self._post_silence_run < self._force_silence_frames:",
     "if self._post_silence_run < self._force_silence_frames + 100:"),
    ("reject_pcm", "audio_admission", "sustained", "server/asr_backlog.py",
     "        total_samples = self._waiting_samples + self._active_samples",
     "        if samples and event.t_start >= 5:\n            self.dropped_segments += 1\n            return Dropped\n        total_samples = self._waiting_samples + self._active_samples"),
    ("stop_asr_worker", "worker_liveness", "overflow", "server/pipeline.py",
     "                        await self._process_segment(event)",
     "                        await self._process_segment(event)\n                        return"),
    ("leave_senders", "stop_completion", "error", "server/pipeline.py",
     "asyncio.gather(*pending, return_exceptions=True), self.roster.aclose(),",
     "asyncio.gather(*pending, return_exceptions=True),"),
)


def mutation_check(source_root):
    results = []
    for name, property_name, probe, relative, before, after in MUTATIONS:
        options = dict(only_sustained=True, seconds=300) if probe == "sustained" else dict(only_witness=probe)
        healthy = asyncio.run(check(**options))
        if healthy["status"] != "PASS":
            raise RuntimeError(f"mutation control is not healthy: {name}: {healthy}")
        with tempfile.TemporaryDirectory(prefix="slow-student-") as directory:
            scratch = Path(directory)
            shutil.copytree(source_root / "server", scratch / "server",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = scratch / relative
            source = target.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"mutation site changed: {name}")
            target.write_text(source.replace(before, after), encoding="utf-8")
            arguments = ["--sustained-only", "--seconds", "300"] if probe == "sustained" else ["--witness", probe]
            child = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                                    "--source-root", str(scratch), *arguments], cwd=scratch,
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
    if not 0 <= args.depth <= 4 or not 20 <= args.seconds <= 3000 or args.seconds % 20:
        parser.error("depth must be 0..4; seconds must be a multiple of 20 in 20..3000")
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
