"""Check the shipping Silero boundary and sustained morph caption delivery."""
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
SYNC_COMMIT = "c7d3851dfb2327db6e9574325b7d957a4c4f2216"
sys.path.insert(0, str(Path(__file__).parent))
from lesson_endurance import Clock, TimedExecutor, clock_scope
from lesson_liveness import ControlledExecutor, Socket, Translation, Violation, callbacks
from morph_lesson import LessonCase, Recognition
from silero_contract import ContractSession, model_scope, run_contract
from silero_contract import MUTATIONS as CONTRACT_MUTATIONS

OPERATIONS = ("clause", "sentence", "boundary", "flush", "asr", "mt")
CHUNKS = (512, 1600, 511)


class SileroLesson(LessonCase):
    def __init__(self, *, chunk=1600, clock=None, service_ticks=8):
        import numpy as np
        from server.config import AppConfig
        from server.inference import InferenceRunner
        from server.pipeline import Pipeline
        from server.roster import Roster

        self.config = AppConfig.model_validate({
            "vad": {"engine": "silero", "threshold": 0.5},
            "turn": {"strategy": "morph", "classifier": "surface"},
            "partial": {"enabled": False},
            "monitoring": {"stats_interval_s": 3600},
            "limits": {"mt_queue_max": 1, "mt_replay_watermark": 1},
        })
        self.fragments, self.events, self.failed, self.trace = {}, [], set(), []
        self.clock, self.chunk = clock, chunk
        self.asr = ControlledExecutor() if clock is None else TimedExecutor(clock, service_ticks)
        self.mt = ControlledExecutor() if clock is None else TimedExecutor(clock, service_ticks)
        self.roster = Roster(self.config.limits)
        self.session = ContractSession(trace=self.trace)
        self.input_tail = np.zeros(0, dtype=np.int16)
        self.input_samples = 0
        with model_scope(self.session):
            self.pipeline = Pipeline(self.roster, self.config, Recognition(self.fragments), Translation(),
                                     asr_runner=InferenceRunner(self.asr), mt_runner=InferenceRunner(self.mt))
        self.teacher = Socket()
        self.students = {lang: Socket() for lang in ("en", "zh")}

    async def ingest(self, pcm):
        import numpy as np

        self.input_samples += len(pcm)
        pending = np.concatenate((self.input_tail, pcm))
        end = len(pending) // 512 * 512
        self.session.expected_frames.extend(pending[i:i + 512].copy() for i in range(0, end, 512))
        self.input_tail = pending[end:].copy()
        self.session.trace = self.trace
        await self.pipeline.feed_audio(pcm.astype("<i2").tobytes())
        self.require(not self.session.expected_frames, "silero_frame_delivery",
                     "all complete 512-sample frames evaluated", len(self.session.expected_frames))
        await callbacks()

    async def pcm_frames(self, identity, frames):
        import numpy as np

        self.trace.append(f"PCM identity={identity}, 32ms frames={frames}")
        pcm = np.full(frames * 512, 1000 + identity if identity else 0, dtype=np.int16)
        for offset in range(0, len(pcm), self.chunk):
            await self.ingest(pcm[offset:offset + self.chunk])

    async def step(self, operation):
        self.trace.append(operation)
        if operation in ("clause", "sentence"):
            await self.pcm_frames(self.fragment(operation), 32)
            await self.pcm_frames(0, 16)
        elif operation == "boundary":
            await self.pcm_frames(0, 16)
            self.events.append("boundary")
        elif operation == "flush":
            await self.flush()
        else:
            executor = self.asr if operation == "asr" else self.mt
            if executor.current is None:
                return False
            executor.complete()
            await callbacks()
        self.workers_alive()
        return True


async def schedule(operations, chunk):
    case = SileroLesson(chunk=chunk)
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


async def segmentation_witness():
    case = SileroLesson()
    try:
        await case.start()
        noise = case.fragment("sentence")
        case.events.pop()
        await case.pcm_frames(noise, 2)
        await case.pcm_frames(0, 32)
        case.require(case.asr.current is None, "silero_start_timing",
                     "64ms speech does not reach the 96ms start threshold", "ASR submitted")
        identity = case.fragment("clause")
        await case.pcm_frames(identity, 32)
        await case.pcm_frames(0, 15)
        case.require(case.asr.current is None, "silero_segment_timing",
                     "480ms does not close a 500ms segment", "ASR submitted early")
        await case.pcm_frames(0, 1)
        case.require(case.asr.current is not None, "silero_segment_timing",
                     "512ms submits ASR", "no ASR")
        case.asr.complete()
        await callbacks()
        await case.pcm_frames(0, 15)
        case.require(not [f for f in case.teacher.frames if f["type"] == "asr_final"],
                     "silero_boundary_timing", "992ms retains the clause", "turn finalized early")
        await case.pcm_frames(0, 1)
        case.events.append("boundary")
        await case.settle()
        await case.pcm_frames(case.fragment("sentence"), 3)
        await case.pcm_frames(0, 16)
        case.require(case.asr.current is not None, "silero_start_timing",
                     "96ms speech is accepted", "no ASR")
        await case.settle()
        return dict(status="PASS", start_speech_frames=3, segment_silence_frames=16,
                    boundary_silence_frames=32)
    finally:
        await case.close()


async def sustained(seconds, service_ticks):
    import numpy as np

    clock = Clock()
    case = SileroLesson(clock=clock, service_ticks=service_ticks)
    deadlines = deque()
    for cycle in range((seconds + 7) // 8):
        for kind in ("clause", "sentence", "clause"):
            case.fragment(kind)
        case.events.append("boundary")
        case.fragment("sentence")
        for index, end_frame in enumerate((96, 184, 240), 1):
            deadlines.append((cycle * 8 + end_frame * .032 + 8, cycle * 3 + index))
    peaks = dict(audio_seconds=0, mt_jobs=0, asr_events=0, pending_samples=0,
                 segment_frames=0, pending_parts=0, history=0, cache=0, receipts=0, tasks=0)
    with clock_scope(clock):
        try:
            await case.start()
            task_bound = len(asyncio.all_tasks()) + 4
            for tick in range(seconds * 10 + 100):
                clock.tick = tick
                case.trace = [f"PCM {min(tick, seconds * 10) / 10}s", f"service {service_ticks / 10}s"]
                case.asr.release_due()
                case.mt.release_due()
                await callbacks()
                if tick < seconds * 10:
                    sample = np.arange(tick * 1600, (tick + 1) * 1600)
                    frame = sample // 512
                    phase = frame % 250
                    fragment = np.select((phase < 32, (48 <= phase) & (phase < 80),
                                          (120 <= phase) & (phase < 152), (192 <= phase) & (phase < 224)),
                                         (1, 2, 3, 4), default=0)
                    pcm = np.where(fragment != 0, 1000 + frame // 250 * 4 + fragment, 0).astype(np.int16)
                    await case.ingest(pcm)
                case.workers_alive()
                p = case.pipeline
                sizes = dict(audio_seconds=p.audio_queue_seconds, mt_jobs=p.captions.queue_depth,
                             asr_events=p.asr_queue_depth, pending_samples=len(p._segmenter._pending),
                             segment_frames=len(p._segmenter._frames), pending_parts=len(p._assembler._pending.parts),
                             history=len(p.captions.history.since(0)), cache=p.captions.metrics().cache.size,
                             receipts=max(len(c.caption_languages) for c in case.roster.students()),
                             tasks=len(asyncio.all_tasks()))
                bounds = dict(audio_seconds=30, mt_jobs=1, asr_events=8, pending_samples=511,
                              segment_frames=64, pending_parts=12, history=50, cache=512,
                              receipts=100, tasks=task_bound)
                case.require(all(sizes[k] <= bounds[k] for k in sizes), "retained_state_bound", bounds, sizes)
                for key, value in sizes.items():
                    peaks[key] = max(peaks[key], value)
                case.require(p.asr_dropped_segments == 0, "audio_admission", 0, p.asr_dropped_segments)
                while deadlines and deadlines[0][0] <= tick / 10:
                    _, count = deadlines.popleft()
                    for lang, socket in case.students.items():
                        actual = sum(f["type"] == "caption" for f in socket.frames)
                        case.require(actual >= count, "sustained_deadline", dict(lang=lang, at_least=count), actual)
            case.compare()
            case.require(not deadlines and p.queue_depth == 0 and case.asr.current is None
                         and case.mt.current is None and not p._assembler.has_pending,
                         "drain_completion", "settled without final flush", "work remains")
            case.require(case.session.calls == seconds * 16000 // 512 and len(case.input_tail) == 0,
                         "silero_frame_delivery", seconds * 16000 // 512, case.session.calls)
            return dict(status="PASS", seconds=seconds, input_chunks=seconds * 10,
                        vad_frames=case.session.calls, service_s=service_ticks / 10,
                        peaks=peaks, **case.summary())
        finally:
            await case.close()


async def check(*, probe=None, seconds=3000):
    report = dict(status="PASS", sync_commit=SYNC_COMMIT, python=platform.python_version(), violations=[])
    def record(error):
        report["status"] = "FAIL"
        if not any(v["property"] == error.detail["property"] for v in report["violations"]):
            report["violations"].append(error.detail)
    if probe in (None, "contract"):
        try:
            report["contract"] = run_contract()
        except Violation as error:
            record(error)
    if probe in (None, "pipeline"):
        attempted = legal = failed = 0
        for chunk in CHUNKS:
            for length in range(4):
                for operations in itertools.product(OPERATIONS, repeat=length):
                    attempted += 1
                    try:
                        legal += bool(await schedule(operations, chunk))
                    except Violation as error:
                        failed += 1
                        error.detail["chunk_samples"] = chunk
                        record(error)
        report["bounded"] = dict(depth=3, chunk_samples=CHUNKS, attempted=attempted,
                                 legal_passed=legal, failed=failed)
    if probe in (None, "timing"):
        try:
            report["timing"] = await segmentation_witness()
        except Violation as error:
            record(error)
    if probe in (None, "sustained"):
        report["sustained"] = []
        for ticks in (2, 8):
            try:
                report["sustained"].append(await sustained(seconds, ticks))
            except Violation as error:
                record(error)
    return report


MUTATIONS = tuple((name, prop, "contract", "server/audio/vad.py", before, after)
                  for name, prop, before, after in CONTRACT_MUTATIONS) + (
    ("wrong_frame_size", "silero_frame_delivery", "timing", "server/pipeline.py",
     "frame_ms=frame_vad.frame_ms,", "frame_ms=100,"),
    ("early_start", "silero_start_timing", "timing", "server/audio/vad.py",
     "self._speech_frames_to_open = max(1, -(-start_speech_ms // frame_ms))",
     "self._speech_frames_to_open = 1"),
    ("early_segment", "silero_segment_timing", "timing", "server/audio/vad.py",
     "max(1, -(-min_silence_ms // frame_ms))", "max(1, min_silence_ms // frame_ms)"),
    ("early_boundary", "silero_boundary_timing", "timing", "server/audio/vad.py",
     "max(1, -(-force_silence_ms // frame_ms))", "max(1, force_silence_ms // frame_ms)"),
    ("drop_translation", "bilingual_delivery", "timing", "server/caption_delivery.py",
     "            await self._enqueue_mt(_LIVE_PRIORITY, MTJob(utterance, lang, closed_at, tuple(ids)))",
     "            pass"),
    ("lose_boundary", "morph_turns", "timing", "server/pipeline.py",
     "else self._assembler.on_boundary()", "else None"),
    ("skip_mt_drain", "drain_completion", "timing", "server/pipeline.py",
     "        await self.captions.drain(timeout)", "        pass"),
    ("stop_on_boundary", "worker_liveness", "timing", "server/pipeline.py",
     "                        await self._close_pending_turn(event)", "                        return"),
    ("late_boundary", "sustained_deadline", "sustained", "server/audio/vad.py",
     "if self._post_silence_run < self._force_silence_frames:",
     "if self._post_silence_run < self._force_silence_frames + 400:"),
    ("unbounded_history", "retained_state_bound", "sustained", "server/caption_history.py",
     "deque(maxlen=maxlen)", "deque()"),
    ("reject_pcm", "audio_admission", "sustained", "server/asr_backlog.py",
     "        total_samples = self._waiting_samples + self._active_samples",
     "        if samples:\n            self.dropped_segments += 1\n            return Dropped\n        total_samples = self._waiting_samples + self._active_samples"),
)


def native_check():
    from silero_native import run_native

    try:
        report = run_native()
    except AssertionError as error:
        report = dict(status="FAIL", violations=[dict(property="native_equivalence",
                      expected="same states and classification as direct ONNX", actual=str(error))])
    report["sync_commit"] = SYNC_COMMIT
    return report


def mutation_check(source_root, *, native=False):
    results = []
    mutations = (("native_normalization", "native_equivalence", "native", "server/audio/vad.py",
                  "frame.astype(np.float32) / 32768.0", "frame.astype(np.float32) / 32767.0"),) if native else MUTATIONS
    for name, property_name, probe, relative, before, after in mutations:
        healthy = native_check() if native else asyncio.run(check(probe=probe, seconds=160))
        if healthy["status"] != "PASS":
            raise RuntimeError(f"mutation control failed: {name}: {healthy}")
        with tempfile.TemporaryDirectory(prefix="silero-lesson-") as directory:
            scratch = Path(directory)
            shutil.copytree(source_root / "server", scratch / "server",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = scratch / relative
            source = target.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"mutation site changed: {name}")
            target.write_text(source.replace(before, after), encoding="utf-8")
            options = ["--native-only"] if native else ["--probe", probe, "--seconds", "160"]
            child = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                                    "--source-root", str(scratch), *options],
                                   cwd=scratch, capture_output=True, encoding="utf-8", timeout=90)
            if child.returncode != 1:
                raise RuntimeError(f"mutation did not return FAIL: {name}: {child.stdout}\n{child.stderr}")
            report = json.loads(child.stdout)
            violation = next((v for v in report["violations"] if v["property"] == property_name), None)
            if report["status"] != "FAIL" or violation is None:
                raise RuntimeError(f"mutation missed {property_name}: {name}: {report}")
            results.append(dict(name=name, healthy="PASS", detected=True, violation=violation))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--seconds", type=int, default=3000)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--probe", choices=("contract", "pipeline", "timing", "sustained"))
    group.add_argument("--native-only", action="store_true")
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if not 8 <= args.seconds <= 3000 or args.seconds % 8:
        parser.error("seconds must be a multiple of 8 in 8..3000")
    sys.path.insert(0, str(args.source_root.resolve()))
    logging.disable(logging.CRITICAL)
    try:
        if args.native_only:
            report = native_check()
        else:
            report = asyncio.run(check(probe=args.probe, seconds=args.seconds))
        if args.mutation_check and report["status"] == "PASS":
            mutations = mutation_check(args.source_root.resolve(), native=args.native_only)
            restored = native_check() if args.native_only else asyncio.run(check(probe=args.probe, seconds=args.seconds))
            if restored != report:
                raise RuntimeError("restored result differs from baseline")
            report.update(mutations=mutations, restored="PASS", restored_matches=True)
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
