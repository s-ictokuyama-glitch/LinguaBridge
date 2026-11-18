"""Explore real Haas decoding and check its PCM-to-caption continuation."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
from dataclasses import dataclass
import hashlib
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
SYNC_COMMIT = "d595f375722d2190746fdbdb3b34de0aacc4d6e8"
BLANK, MAX_SYMBOLS = 3072, 10
DURATIONS = (0, 1, 2, 3, 4)
DECODING_SHA256 = "e535e51bed8729c3d1c5acb5d016feae774f8b13160f2dccbcd4841ad6ecd5be"
SOURCE = "server/asr/haas_onnx_engine.py"
sys.path.insert(0, str(Path(__file__).parent))
from lesson_liveness import ControlledExecutor, Socket, Translation, Violation, callbacks


@dataclass(frozen=True)
class Decision:
    token: int
    duration: int


CHOICES = tuple(Decision(token, duration) for token in (257, BLANK) for duration in DURATIONS)


class NeedDecision(Exception):
    pass


def require(condition, name, expected, actual, trace):
    if not condition:
        raise Violation(name, expected, actual, trace)


class Sessions:
    def reset(self, frames, decisions):
        self.frames, self.decisions = frames, decisions
        self.trace = []
        self.predicted = []
        self.state = (7., 11., 13.)

    def run(self, outputs, inputs):
        if outputs[0] == "pred_proj":
            token = int(inputs["token"][0])
            actual = (float(inputs["kda_state"].item()), float(inputs["conv_state"].item()))
            require(actual == self.state[1:], "prediction_state", self.state[1:], actual, self.trace)
            self.predicted.append(token)
            self.state = tuple((value * 3 + token) % 1000003 for value in self.state)
            return tuple(np.array([[value]]) for value in self.state)

        frame = int(inputs["enc_frame"][0, 0])
        visits = Counter(item["frame"] for item in self.trace)
        require(len(self.trace) < self.frames * MAX_SYMBOLS and visits[frame] < MAX_SYMBOLS,
                "decode_progress", f"at most {MAX_SYMBOLS} calls per frame", frame, self.trace)
        if self.trace:
            previous = self.trace[-1]
            advance = (previous["duration"] > 0 or previous["token"] == BLANK
                       or visits[previous["frame"]] == MAX_SYMBOLS)
            expected = previous["frame"] + (max(1, previous["duration"]) if advance else 0)
        else:
            expected = 0
        require(frame == expected and 0 <= frame < self.frames,
                "frame_order", expected, frame, self.trace)
        projection = float(inputs["pred_proj"].item())
        require(projection == self.state[0], "prediction_state", self.state[0], projection, self.trace)
        if len(self.trace) == len(self.decisions):
            raise NeedDecision
        decision = self.decisions[len(self.trace)]
        self.trace.append(dict(frame=frame, token=decision.token, duration=decision.duration))
        scores = np.full((1, BLANK + 1 + len(DURATIONS)), -10., dtype=np.float32)
        scores[0, decision.token] = 10
        scores[0, BLANK + 1 + DURATIONS.index(decision.duration)] = 10
        return (scores,)


def decoder():
    from server.asr.haas_onnx_engine import _HaasDecoder

    result = object.__new__(_HaasDecoder)
    sessions = Sessions()
    result._blank, result._durations, result._max_symbols = BLANK, DURATIONS, MAX_SYMBOLS
    result._joint = result._prediction = sessions
    result._start = tuple(np.array([[value]]) for value in (7., 11., 13.))
    result._pieces = [""] * BLANK
    for token, text in {0: "<unk>", 257: "です", 590: "。", 1109: "光"}.items():
        result._pieces[token] = text
    result._unknown = 0
    result._window = np.hanning(512)
    result._mel_filter = np.ones((4, 257), dtype=np.float32) / 257
    result._preemphasis, result._log_floor, result._pad_to = np.float32(.97), np.float32(1e-6), 16
    return result, sessions


def decode_once(engine, sessions, frames, decisions):
    sessions.reset(frames, decisions)
    encoded = np.arange(frames, dtype=np.float32).reshape(-1, 1)
    tokens = list(engine._decode(encoded))
    expected = [entry["token"] for entry in sessions.trace if entry["token"] != BLANK]
    require(tokens == expected and sessions.predicted == expected,
            "token_preservation", expected, dict(emitted=tokens, predicted=sessions.predicted), sessions.trace)
    if sessions.trace:
        last = sessions.trace[-1]
        visits = sum(item["frame"] == last["frame"] for item in sessions.trace)
        advance = last["duration"] > 0 or last["token"] == BLANK or visits == MAX_SYMBOLS
        require(advance and last["frame"] + max(1, last["duration"]) >= frames,
                "decode_completion", "all encoded frames consumed", last, sessions.trace)
    else:
        require(frames == 0, "decode_completion", "nonempty input decoded", frames, [])
    start = [float(value.item()) for value in engine._start]
    require(start == [7., 11., 13.], "utterance_reset", [7., 11., 13.], start, sessions.trace)
    return tokens


def explore():
    counts = {}
    for frames in range(3):
        pending = deque([()])
        complete = 0
        engine, sessions = decoder()
        while pending:
            decisions = pending.popleft()
            try:
                tokens = decode_once(engine, sessions, frames, decisions)
            except NeedDecision:
                pending.extend(decisions + (choice,) for choice in CHOICES)
                continue
            require(decode_once(engine, sessions, frames, decisions) == tokens,
                    "utterance_reset", tokens, "different repeat", sessions.trace)
            complete += 1
        counts[str(frames)] = complete
    require(counts == {"0": 1, "1": 91, "2": 2881}, "exploration_coverage",
            {"0": 1, "1": 91, "2": 2881}, counts, [])
    return dict(terminal_schedules=sum(counts.values()), by_frames=counts, repeated=True)


def witnesses():
    engine, sessions = decoder()
    plans = [
        (4, (Decision(BLANK, 0),) * 4),
        (4, (Decision(257, 0),) * 40),
        (5, (Decision(1109, 4), Decision(590, 1))),
        (3, (Decision(1109, 0), Decision(257, 0), Decision(590, 1),
             Decision(BLANK, 0), Decision(BLANK, 0))),
    ]
    for frames, decisions in plans * 2:
        decode_once(engine, sessions, frames, decisions)
    text = engine._text(iter([1109, 257, 590]))
    require(text == "光です。", "token_text", "光です。", text, [])
    return dict(decodes=len(plans) * 2, mixed_token_text=text)


def chunks():
    from server.asr.haas_onnx_engine import _chunks, HaasOnnxEngine

    count = 0
    lengths = (1, 319, 320, 448000 - 1, 448000, 448000 + 1,
               480000 - 1, 480000, 480000 + 1, 896000, 960001)
    for length, shape in itertools.product(lengths, ("ramp", "constant", "quiet")):
        pcm = (np.arange(length, dtype=np.int64) % 65536 - 32768).astype(np.int16)
        if shape == "constant":
            pcm[:] = 1234
        elif shape == "quiet":
            pcm[430000:435000] = 0
        parts = list(_chunks(pcm))
        trace = [dict(samples=length, shape=shape)]
        require(all(0 < len(part) <= 480000 for part in parts),
                "chunk_bound", "nonempty chunks <= 30 seconds", [len(p) for p in parts], trace)
        actual = np.concatenate(parts)
        require(np.array_equal(actual, pcm), "pcm_partition", length, len(actual), trace)

        class Recorder:
            def __init__(self):
                self.audio = []

            def transcribe(self, audio):
                self.audio.append(audio.copy())
                return f"[{len(self.audio)}]"

        recorder = Recorder()
        engine = HaasOnnxEngine(Path("unused-model"))
        engine._decoder = recorder
        result = engine.transcribe(pcm, 16000)
        expected = pcm.astype(np.float32) / 32768.
        captured = np.concatenate(recorder.audio)
        require(np.array_equal(captured, expected), "pcm_normalization",
                dict(samples=length, first=float(expected[0])),
                dict(samples=len(captured), first=float(captured[0])), trace)
        text = "".join(f"[{i}]" for i in range(1, len(recorder.audio) + 1))
        require(result.text == text, "chunk_text", text, result.text, trace)
        count += 1
    return dict(cases=count, lengths=list(lengths), shapes=["ramp", "constant", "quiet"])


class Encoder:
    def __init__(self, sessions, plans):
        self.sessions, self.plans = sessions, deque(plans)

    def run(self, outputs, inputs):
        if not self.plans:
            raise RuntimeError("unexpected extra encoder call")
        require(np.isfinite(inputs["mel"]).all(), "finite_features", "finite mel", "NaN/inf", [])
        frames, decisions = self.plans.popleft()
        self.sessions.reset(frames, decisions)
        return np.arange(frames, dtype=np.float32).reshape(1, frames, 1), np.array([frames])


async def classroom():
    from server.asr.haas_onnx_engine import HaasOnnxEngine
    from server.config import AppConfig
    from server.inference import InferenceRunner
    from server.pipeline import Pipeline
    from server.roster import Roster

    config = AppConfig.model_validate({
        "vad": {"engine": "energy", "threshold": 300, "min_silence_ms": 100, "pre_roll_ms": 0},
        "turn": {"strategy": "morph", "classifier": "surface"},
        "monitoring": {"stats_interval_s": 3600},
        "limits": {"mt_queue_max": 1, "mt_replay_watermark": 1},
    })
    voice = (Decision(1109, 0), Decision(257, 0), Decision(590, 1))
    stalled = (Decision(257, 0),) * 10 + (Decision(590, 1),)
    plans = [(1, voice), (2, (Decision(BLANK, 0),) * 2), (2, stalled)] * 4 + [(1, voice)]
    expected = [text for text in (["光です。", "", "です" * 10 + "。"] * 4 + ["光です。"]) if text]
    engine, sessions = decoder()
    encoder = Encoder(sessions, plans)
    engine._encoder = encoder
    asr_engine = HaasOnnxEngine(Path("unused-model"))
    asr_engine._decoder = engine
    asr, mt = ControlledExecutor(), ControlledExecutor()
    roster = Roster(config.limits)
    pipeline = Pipeline(roster, config, asr_engine, Translation(),
                        asr_runner=InferenceRunner(asr), mt_runner=InferenceRunner(mt))
    teacher = Socket()
    students = {lang: Socket() for lang in ("en", "zh")}
    trace = []
    try:
        await roster.add(teacher, "teacher")
        for lang, socket in students.items():
            await roster.add(socket, "student", lang)
        await pipeline.start()
        await callbacks()
        require(pipeline.ready, "caption_continuation", "ready", pipeline.ready, trace)
        await pipeline.set_accepting_audio(True)
        for index in range(len(plans)):
            trace.append(f"PCM utterance {index + 1}")
            for value in (1001 + index, 1001 + index, 0, 0, 0, 0, 0):
                await pipeline.feed_audio(np.full(1600, value, dtype="<i2").tobytes())
            await callbacks()
            for executor in (asr, mt):
                if executor.current is not None:
                    executor.complete()
            await callbacks()
        drain = asyncio.create_task(pipeline.drain(timeout=None))
        try:
            for _ in range(200):
                for executor in (asr, mt):
                    if executor.current is not None:
                        executor.complete()
                await callbacks()
                workers = [*pipeline._tasks, pipeline.captions._worker_task]
                require(all(t is not None and not t.done() for t in workers),
                        "caption_continuation", "workers alive", "worker stopped", trace)
                if drain.done():
                    await drain
                    break
            require(drain.done(), "caption_continuation", "drain completed", "pending", trace)
        finally:
            if not drain.done():
                drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
        require(not encoder.plans and pipeline.asr_dropped_segments == 0,
                "caption_continuation", "all PCM processed", len(encoder.plans), trace)
        actual = [f["ja"] for f in teacher.frames if f["type"] == "asr_final"]
        require(actual == expected, "caption_continuation", expected, actual, trace)
        for lang, socket in students.items():
            actual = [(f["seq"], f["ja"], f["text"], f["lang"])
                      for f in socket.frames if f["type"] == "caption"]
            wanted = [(i, text, f"{lang}:{text}", lang) for i, text in enumerate(expected, 1)]
            require(actual == wanted and socket.closed is None, "caption_continuation", wanted, actual, trace)
        return dict(pcm_utterances=len(plans), empty_results=4, captions_per_language=len(expected),
                    final_continuation=True, asr_calls=pipeline.asr_calls)
    finally:
        await pipeline.stop()


def check(probe=None):
    report = dict(status="PASS", sync_commit=SYNC_COMMIT, python=platform.python_version(), violations=[])
    checks = {"decode": explore, "witness": witnesses, "chunks": chunks,
              "classroom": lambda: asyncio.run(classroom())}
    for name, operation in checks.items():
        if probe is not None and name != probe:
            continue
        try:
            report[name] = operation()
        except Violation as error:
            report["status"] = "FAIL"
            report["violations"].append(error.detail)
    return report


MUTATIONS = (
    ("no_symbol_limit", "decode_progress", "witness", "or symbols_at_frame >= self._max_symbols", "or False"),
    ("blank_does_not_advance", "frame_order", "witness", "frame += max(1, duration)", "frame += duration"),
    ("ignore_duration", "frame_order", "witness", "frame += max(1, duration)", "frame += 1"),
    ("carry_prediction_state", "utterance_reset", "witness", "symbols_at_frame += 1", "symbols_at_frame += 1\n                self._start = projection, kda, conv"),
    ("emit_blank", "token_preservation", "decode", "if token != self._blank:", "if True:"),
    ("stop_early", "decode_completion", "decode", "while frame < len(encoded):", "while frame < len(encoded) - 1:"),
    ("drop_text_piece", "token_text", "witness", "return \"\".join(text).strip()", "return \"\".join(text[1:]).strip()"),
    ("drop_pcm", "pcm_partition", "chunks", "audio = audio[cut:]", "audio = audio[cut + 1:]"),
    ("duplicate_pcm", "pcm_partition", "chunks", "audio = audio[cut:]", "audio = audio[cut - 1:]"),
    ("wrong_pcm_scale", "pcm_normalization", "chunks", "pcm16.astype(np.float32) / 32768.0", "pcm16.astype(np.float32) / 16384.0"),
    ("drop_chunk_text", "chunk_text", "chunks", "text = \"\".join(self._decoder.transcribe(chunk) for chunk in _chunks(audio))", "text = \"\".join(self._decoder.transcribe(chunk) for chunk in _chunks(audio))[1:]"),
    ("drop_recognition", "caption_continuation", "classroom", "return ASRResult(text=text) if text", "return ASRResult(text='') if text"),
)


def mutation_check(source_root):
    results = []
    for name, property_name, probe, before, after in MUTATIONS:
        with tempfile.TemporaryDirectory(prefix="haas-decode-") as directory:
            scratch = Path(directory)
            shutil.copytree(source_root / "server", scratch / "server",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = scratch / SOURCE
            source = target.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"mutation site changed: {name}")
            target.write_text(source.replace(before, after), encoding="utf-8")
            child = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                                    "--source-root", str(scratch), "--probe", probe],
                                   cwd=scratch, capture_output=True, encoding="utf-8", timeout=45)
            if child.returncode != 1:
                raise RuntimeError(f"mutation did not return FAIL: {name}: {child.stdout}\n{child.stderr}")
            report = json.loads(child.stdout)
            violation = next((v for v in report["violations"] if v["property"] == property_name), None)
            if report["status"] != "FAIL" or violation is None:
                raise RuntimeError(f"mutation missed {property_name}: {name}: {report}")
            results.append(dict(name=name, detected=True, violation=violation))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--probe", choices=("decode", "witness", "chunks", "classroom"))
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    sys.path.insert(0, str(source_root))
    logging.disable(logging.CRITICAL)
    try:
        from server.asr import haas_onnx_engine
        if Path(haas_onnx_engine.__file__).resolve() != source_root / SOURCE:
            raise RuntimeError("wrong product source imported")
        digest = hashlib.sha256((source_root / SOURCE).read_bytes()).hexdigest()
        report = check(args.probe)
        if args.mutation_check:
            if args.probe:
                raise RuntimeError("mutation-check requires the full baseline")
            mutations = mutation_check(source_root)
            restored = check()
            if restored != report or hashlib.sha256((source_root / SOURCE).read_bytes()).hexdigest() != digest:
                raise RuntimeError("restored result or product source differs from baseline")
            report.update(mutations=mutations, restored_matches=True)
        report.update(source_sha256=digest, decoding_sha256=DECODING_SHA256,
                      bounds=dict(encoded_frames=[0, 1, 2], tokens=[257, BLANK],
                                  durations=DURATIONS, max_symbols=MAX_SYMBOLS))
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
