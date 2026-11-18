"""Enumerate long-speech split boundaries and replay 50 minutes through real captions."""
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

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SYNC_COMMIT = "ea925f9ed3be791d96e24faa975a00ec4c81d99c"
sys.path.insert(0, str(Path(__file__).parent))
from lesson_endurance import Clock, TimedExecutor, clock_scope
from lesson_liveness import ControlledExecutor, Scenario, Socket, Translation, Violation, callbacks
from silero_contract import ContractSession, model_scope

FRAME = 512
TRIGGER = 938
PERIODS = (876, 907, 938)
CHUNKS = (511, 512, 1600)
RELEASES = ("eager", "hold_asr", "hold_mt")


def waveform(start, count, period):
    positions = np.arange(start, start + count, dtype=np.int64)
    mixed = (positions * 1103515245 + (positions // 65521) * 12345) & 0x7fffffff
    quiet = (positions // FRAME + 1) % period == 0
    return np.where(quiet, 50 + mixed % 19, 2000 + mixed % 2000).astype(np.int16)


def spans(frames, period, *, origin=0):
    ends = list(range(period, frames - TRIGGER + period + 1, period))
    bounds = [origin, *(origin + end * FRAME for end in ends)]
    if bounds[-1] != origin + frames * FRAME:
        bounds.append(origin + frames * FRAME)
    return list(zip(bounds, bounds[1:]))


def words(start, end):
    return f"【{start}:{end}】温度が高いので"


def turns(parts, *, flush):
    confirmed, pending, size = [], [], 0
    for start, end in parts:
        pending.append(words(start, end))
        size += end - start
        if size >= 480000:
            confirmed.append("".join(pending))
            pending, size = [], 0
    if pending and flush:
        confirmed.append("".join(pending))
    return confirmed


class Recognition:
    def __init__(self, case):
        self.case = case
        self.seen = []
        self.error = None

    def warmup(self):
        pass

    def transcribe(self, pcm, sample_rate):
        from server.asr.base import ASRResult

        index = len(self.seen)
        try:
            self.case.require(index < len(self.case.parts), "pcm_partition", "planned ASR slice", index)
            start, end = self.case.parts[index]
            expected = waveform(start, end - start, self.case.period)
            self.case.require(sample_rate == 16000 and np.array_equal(expected, pcm), "pcm_partition",
                              dict(start=start, end=end, samples=end - start),
                              dict(samples=len(pcm), head=pcm[:4].tolist(), tail=pcm[-4:].tolist()))
            self.seen.append((start, end))
            return ASRResult(words(start, end))
        except Violation as error:
            self.error = error
            raise


class Classroom(Scenario):
    def __init__(self, frames, period, *, clock=None):
        from server.config import AppConfig
        from server.inference import InferenceRunner
        from server.pipeline import Pipeline
        from server.roster import Roster

        self.frames, self.period, self.clock = frames, period, clock
        self.parts = spans(frames, period)
        self.expected_turns = turns(self.parts, flush=True)
        self.trace = [f"frames={frames}, quiet_period={period}"]
        self.input_samples = 0
        self.tail = np.zeros(0, dtype=np.int16)
        self.session = ContractSession(trace=self.trace)
        self.recognition = Recognition(self)
        self.config = AppConfig.model_validate({
            "vad": {"engine": "silero", "threshold": 0.5},
            "turn": {"strategy": "morph", "classifier": "surface"},
            "partial": {"enabled": False}, "monitoring": {"stats_interval_s": 3600},
            "limits": {"mt_queue_max": 1, "mt_replay_watermark": 1},
        })
        self.asr = ControlledExecutor() if clock is None else TimedExecutor(clock, 2)
        self.mt = ControlledExecutor() if clock is None else TimedExecutor(clock, 2)
        self.roster = Roster(self.config.limits)
        with model_scope(self.session):
            self.pipeline = Pipeline(self.roster, self.config, self.recognition, Translation(),
                                     asr_runner=InferenceRunner(self.asr), mt_runner=InferenceRunner(self.mt))
        self.teacher = Socket()
        self.students = {lang: Socket() for lang in ("en", "zh")}

    def health(self):
        if self.recognition.error is not None:
            raise self.recognition.error
        self.workers_alive()
        self.require(self.pipeline.asr_dropped_segments == 0, "audio_admission", 0,
                     self.pipeline.asr_dropped_segments)
        retained = len(self.pipeline._segmenter._frames)
        self.require(retained < TRIGGER, "segment_bound", f"fewer than {TRIGGER} frames", retained)

    async def ingest(self, size):
        pcm = waveform(self.input_samples, size, self.period)
        self.input_samples += size
        pending = np.concatenate((self.tail, pcm))
        end = len(pending) // FRAME * FRAME
        self.session.expected_frames.extend(pending[i:i + FRAME].copy() for i in range(0, end, FRAME))
        self.tail = pending[end:].copy()
        await self.pipeline.feed_audio(pcm.astype("<i2").tobytes())
        await callbacks()
        self.health()
        self.require(not self.session.expected_frames, "silero_frame_delivery", 0,
                     len(self.session.expected_frames))

    async def release(self, *, mt=True):
        for executor in (self.asr, self.mt) if mt else (self.asr,):
            if executor.current is not None:
                executor.complete()
                await callbacks()
                self.health()

    def compare(self, expected=None, property_name="bilingual_delivery"):
        expected = self.expected_turns if expected is None else expected
        wanted = list(enumerate(expected, 1))
        actual = [(f["seq"], f["ja"]) for f in self.teacher.frames if f["type"] == "asr_final"]
        self.compare_sequence("turn_progress", wanted, actual)
        for lang, socket in self.students.items():
            actual = [(f["seq"], f["ja"], f["text"], f["lang"])
                      for f in socket.frames if f["type"] == "caption"]
            self.compare_sequence(property_name, [(seq, text, f"{lang}:{text}", lang)
                                                  for seq, text in wanted], actual)
            self.require(socket.closed is None, "bilingual_delivery", None, socket.closed)

    async def settle(self, *, flush):
        if flush:
            await self.pipeline.flush_audio()
            self.tail = self.tail[:0].copy()
            self.session.reset_expected()
        drain = asyncio.create_task(self.pipeline.drain(timeout=None))
        try:
            for _ in range(6 * (len(self.parts) + 3)):
                await callbacks()
                self.health()
                if drain.done():
                    await drain
                    remaining = self.outstanding()
                    self.require(not any(remaining.values()), "drain_completion", "all work complete", remaining)
                    return
                await self.release()
            self.require(False, "drain_completion", "drain returns after inference completes", self.outstanding())
        finally:
            if not drain.done():
                drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)

    async def finish_and_continue(self):
        await self.settle(flush=True)
        self.require(self.recognition.seen == self.parts, "pcm_partition", self.parts, self.recognition.seen)
        self.compare()
        start = self.input_samples
        self.parts.append((start, start + 32 * FRAME))
        self.expected_turns.append(words(start, start + 32 * FRAME))
        await self.ingest(32 * FRAME)
        await self.settle(flush=True)
        self.compare()
        self.require(self.recognition.seen == self.parts, "pcm_partition", self.parts, self.recognition.seen)

    async def close(self):
        await self.pipeline.stop()


async def boundary_case(period, frames, chunk, release):
    case = Classroom(frames, period)
    case.trace.append(f"chunk={chunk}, release={release}")
    try:
        await case.start()
        for start in range(0, frames * FRAME, chunk):
            await case.ingest(min(chunk, frames * FRAME - start))
            if release != "hold_asr" or case.input_samples >= (period + TRIGGER - 2) * FRAME:
                await case.release(mt=release != "hold_mt")
        for _ in range(8):
            await case.release()
        forced = case.parts[:max(0, (frames - TRIGGER) // period + 1)]
        case.require(case.recognition.seen == forced, "pcm_partition", forced, case.recognition.seen)
        case.compare(turns(forced, flush=False))
        await case.finish_and_continue()
        return dict(asr_calls=case.pipeline.asr_calls, captions=len(case.expected_turns))
    finally:
        await case.close()


async def sustained(seconds):
    clock = Clock()
    frames = seconds * 16000 // FRAME
    case = Classroom(frames, 907, clock=clock)
    deadlines = deque()
    for index in range(1, len(case.parts)):
        if index % 2 == 0:
            deadlines.append(((907 * (index - 1) + TRIGGER) * .032 + 2, index // 2))
    peaks = dict(audio_seconds=0, mt_jobs=0, segment_frames=0, pending_parts=0, history=0, cache=0)
    with clock_scope(clock):
        try:
            await case.start()
            for tick in range(seconds * 10 + 20):
                clock.tick = tick
                case.trace[:] = [f"continuous PCM {min(tick, seconds * 10) / 10}s"]
                case.asr.release_due()
                case.mt.release_due()
                await callbacks()
                if tick < seconds * 10:
                    await case.ingest(1600)
                case.health()
                p = case.pipeline
                sizes = dict(audio_seconds=p.audio_queue_seconds, mt_jobs=p.captions.queue_depth,
                             segment_frames=len(p._segmenter._frames), pending_parts=len(p._assembler._pending.parts),
                             history=len(p.captions.history.since(0)), cache=p.captions.metrics().cache.size)
                bounds = dict(audio_seconds=30.016, mt_jobs=1, segment_frames=937, pending_parts=1,
                              history=50, cache=512)
                case.require(all(sizes[k] <= bounds[k] for k in sizes), "retained_state_bound", bounds, sizes)
                for key, value in sizes.items():
                    peaks[key] = max(peaks[key], value)
                while deadlines and deadlines[0][0] <= tick / 10:
                    _, count = deadlines.popleft()
                    for lang, socket in case.students.items():
                        actual = sum(f["type"] == "caption" for f in socket.frames)
                        case.require(actual >= count, "sustained_progress", dict(lang=lang, at_least=count), actual)
            case.require(not deadlines, "sustained_progress", "all streaming deadlines observed", list(deadlines))
            await case.finish_and_continue()
            return dict(status="PASS", seconds=seconds, input_chunks=seconds * 10,
                        asr_service_s=.2, mt_service_s=.2, peaks=peaks,
                        asr_calls=case.pipeline.asr_calls, captions_per_language=len(case.expected_turns),
                        continuation=True)
        finally:
            await case.close()


async def check(probe=None, seconds=3000):
    report = dict(status="PASS", sync_commit=SYNC_COMMIT, python=platform.python_version(), violations=[])

    def record(error):
        report["status"] = "FAIL"
        if not any(v["property"] == error.detail["property"] for v in report["violations"]):
            report["violations"].append(error.detail)

    if probe in (None, "bounded"):
        count = 0
        failures = []
        for period, chunk, release in itertools.product(PERIODS, CHUNKS, RELEASES):
            for frames in (937, 938, 939, period + 937, period + 938, period + 939):
                try:
                    await boundary_case(period, frames, chunk, release)
                except Violation as error:
                    record(error)
                    failures.append(dict(frames=frames, quiet_period=period, chunk=chunk,
                                         release=release, property=error.detail["property"]))
                count += 1
        report["bounded"] = dict(cases=count, passed=count - len(failures), failed=len(failures),
                                 failures=failures,
                                 quiet_period_frames=PERIODS, chunk_samples=CHUNKS,
                                 release_schedules=RELEASES, trigger_frames=TRIGGER)
    try:
        if probe == "witness":
            report["witness"] = await boundary_case(907, 1846, 511, "hold_mt")
        if probe in (None, "sustained"):
            report["sustained"] = await sustained(seconds)
    except Violation as error:
        record(error)
    return report


MUTATIONS = (
    ("drop_carry", "pcm_partition", "witness", "server/audio/vad.py", "self._frames = carried", "self._frames = []"),
    ("duplicate_carry", "segment_bound", "witness", "server/audio/vad.py", "carried = self._frames[keep:]", "carried = self._frames[keep - 1:]"),
    ("late_split", "segment_bound", "witness", "server/audio/vad.py", "self._offset - self._utt_start_sample < self._max_samples", "self._offset - self._utt_start_sample < self._max_samples * 2"),
    ("lose_turn_limit", "turn_progress", "witness", "server/turn/assembler.py", "or self._pending.span_s() >= self._max_turn_s", "or False"),
    ("drop_translation", "bilingual_delivery", "witness", "server/caption_delivery.py", "            await self._enqueue_mt(_LIVE_PRIORITY, MTJob(utterance, lang, closed_at, tuple(ids)))", "            pass"),
    ("skip_mt_drain", "drain_completion", "witness", "server/pipeline.py", "        await self.captions.drain(timeout)", "        pass"),
    ("stop_streaming_translation", "sustained_progress", "sustained", "server/caption_delivery.py", "            await self._enqueue_mt(_LIVE_PRIORITY, MTJob(utterance, lang, closed_at, tuple(ids)))", "            pass"),
    ("retain_turn_parts", "retained_state_bound", "sustained", "server/turn/assembler.py", "or self._pending.span_s() >= self._max_turn_s", "or False"),
    ("reject_long_audio", "audio_admission", "sustained", "server/asr_backlog.py", "        total_samples = self._waiting_samples + self._active_samples", "        if samples:\n            self.dropped_segments += 1\n            return Dropped\n        total_samples = self._waiting_samples + self._active_samples"),
)


def mutation_check(source_root):
    results = []
    for probe in ("witness", "sustained"):
        healthy = asyncio.run(check(probe, seconds=64))
        if healthy["status"] != "PASS":
            raise RuntimeError(f"mutation control failed: {healthy}")
    for name, property_name, probe, relative, before, after in MUTATIONS:
        with tempfile.TemporaryDirectory(prefix="continuous-speech-") as directory:
            scratch = Path(directory)
            shutil.copytree(source_root / "server", scratch / "server",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copy2(source_root / "config.yaml", scratch / "config.yaml")
            target = scratch / relative
            source = target.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"mutation site changed: {name}")
            target.write_text(source.replace(before, after), encoding="utf-8")
            child = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                                    "--source-root", str(scratch), "--probe", probe, "--seconds", "64"],
                                   cwd=scratch, capture_output=True, encoding="utf-8", timeout=90)
            if child.returncode != 1:
                raise RuntimeError(f"mutation did not return FAIL: {name}: {child.stdout}\n{child.stderr}")
            report = json.loads(child.stdout)
            violation = next((v for v in report["violations"] if v["property"] == property_name), None)
            if report["status"] != "FAIL" or violation is None:
                raise RuntimeError(f"mutation missed {property_name}: {name}: {report}")
            results.append(dict(name=name, healthy="PASS", detected=True, violation=violation))
    return results


def configuration(source_root):
    import yaml
    from server.config import AppConfig

    expected = {"vad.engine": "silero", "vad.threshold": .5, "vad.max_utterance_s": 30,
                "turn.strategy": "morph", "turn.classifier": "surface", "turn.morph.max_turn_s": 30.,
                "turn.morph.max_segments": 12, "turn.morph.start_speech_ms": 96, "partial.enabled": False}
    sources = {"defaults": AppConfig().model_dump(),
               "config.yaml": yaml.safe_load((source_root / "config.yaml").read_text(encoding="utf-8"))}
    for name, source in sources.items():
        actual = {}
        for path in expected:
            value = source
            for key in path.split("."):
                value = value[key]
            actual[path] = value
        if actual != expected:
            raise RuntimeError(f"shipping configuration changed in {name}: expected {expected}, actual {actual}")
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--probe", choices=("bounded", "witness", "sustained"))
    parser.add_argument("--seconds", type=int, default=3000)
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if not 64 <= args.seconds <= 3000 or args.seconds % 4:
        parser.error("seconds must be a multiple of 4 in 64..3000")
    sys.path.insert(0, str(args.source_root.resolve()))
    logging.disable(logging.CRITICAL)
    try:
        settings = configuration(args.source_root.resolve())
        report = asyncio.run(check(args.probe, args.seconds))
        if args.mutation_check:
            mutations = mutation_check(args.source_root.resolve())
            restored = asyncio.run(check(args.probe, args.seconds))
            if restored != report:
                raise RuntimeError("restored result differs from baseline")
            report.update(mutations=mutations, restored=restored["status"], restored_matches=True)
        report["configuration"] = settings
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
