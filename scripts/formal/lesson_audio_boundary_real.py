"""Compare two lessons with identical post-restart speech using the shipped models."""
from __future__ import annotations

import argparse
import asyncio
from functools import partial
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import sys
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts/formal")]
PACKET = 3200  # 100 ms, mono PCM16 at 16 kHz
FRAME = 1024  # 32 ms Silero input, bytes


def read_fixture(name):
    with wave.open(str(ROOT / "tests/fixtures/ja" / f"{name}.wav")) as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
            raise ValueError(f"{name}.wav requires mono 16 kHz PCM16")
        return source.readframes(source.getnframes())


def sample_offset(pcm, marker):
    offset = pcm.find(marker)
    while offset >= 0 and offset % 2:
        offset = pcm.find(marker, offset + 1)
    return offset // 2


def timestamp_samples(seconds):
    samples = seconds * 16000
    nearest = round(samples)
    if not math.isclose(samples, nearest, rel_tol=0, abs_tol=1e-6):
        raise ValueError(f"utterance timestamp is not on the 16 kHz sample grid: {seconds}")
    return nearest


async def replay(config, prefix, markers, speech, action):
    from lesson_liveness_real import Socket
    from server.main import create_app
    from server.publication import Publication

    app = create_app(config, Publication(config.server))
    lesson, pipeline = app.state.lesson, app.state.pipeline
    teacher = Socket()
    students = {lang: Socket() for lang in ("en", "zh")}
    observed = []
    runner = pipeline._asr_runner
    run = runner.run

    async def observe(operation, *, timeout):
        if not isinstance(operation, partial) or operation.func != pipeline._asr.transcribe:
            raise RuntimeError("ASR runner contract changed after warmup")
        pcm, rate = operation.args
        if rate != 16000:
            raise RuntimeError(f"unexpected ASR input rate {rate}")
        raw = pcm.tobytes()
        observed.append({
            "samples": len(pcm), "sha256": hashlib.sha256(raw).hexdigest(),
            "head_pcm16": pcm[:8].tolist(),
            "old_frame_offsets": [sample_offset(raw, marker) for marker in markers],
        })
        return await run(operation, timeout=timeout)

    result = {}
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(pipeline._warmup_task, timeout=180)
        if not pipeline.ready:
            raise RuntimeError("model warmup failed")
        await pipeline.roster.add(teacher, "teacher")
        for lang, socket in students.items():
            await pipeline.roster.add(socket, "student", lang)
        with patch.object(runner, "run", observe):
            await lesson.control("start")
            if lesson.state != "live":
                raise RuntimeError("first lesson did not start")
            if prefix:
                await pipeline.feed_audio(prefix)
                if pipeline._segmenter.is_open:
                    raise ValueError("old fixture opened a Segment before the stop")
            await lesson.control(action)
            if lesson.state != {"pause": "paused", "end": "ended"}[action]:
                raise RuntimeError(f"{action} left the lesson in state {lesson.state}")
            await lesson.control("start")
            if observed or lesson.state != "live":
                raise RuntimeError("restart requires live state and no earlier ASR")
            for name, pcm in speech.items():
                teacher_at = len(teacher.frames)
                student_at = {lang: len(socket.frames) for lang, socket in students.items()}
                start, mt = len(observed), pipeline.captions.metrics().calls
                utterance_at = pipeline.captions.history.seq_head
                packets = pcm + bytes(-len(pcm) % PACKET + 12 * PACKET)
                for offset in range(0, len(packets), PACKET):
                    await pipeline.feed_audio(packets[offset:offset + PACKET])
                await pipeline.drain(timeout=180)
                await asyncio.wait_for(asyncio.gather(*(
                    sender._queue.join() for sender in pipeline.roster._senders.values())), timeout=10)
                finals = [[m["seq"], m["ja"]] for m in teacher.frames[teacher_at:]
                          if m["type"] == "asr_final"]
                captions = {
                    lang: [[m["seq"], m["ja"], m["lang"], m["text"]]
                           for m in socket.frames[student_at[lang]:] if m["type"] == "caption"]
                    for lang, socket in students.items()
                }
                delivered = bool(finals) and all(
                    [m[:2] for m in messages] == finals and all(m[2] == lang and m[3].strip() for m in messages)
                    for lang, messages in captions.items())
                result[name] = dict(asr=observed[start:], finals=finals, captions=captions, delivered=delivered,
                                    mt_calls=pipeline.captions.metrics().calls - mt,
                                    utterances=[{
                                        "t_start": u.t_start, "t_end": u.t_end,
                                    } for u in pipeline.captions.history.since(utterance_at)])
    return result


async def check(models, action="end"):
    import numpy as np
    from server.audio.vad import SileroVAD
    from server.config import load_config

    config = load_config(ROOT / "config.yaml")
    if models:
        config.models.dir = str(models.resolve())
    if (config.vad.engine, config.turn.strategy, config.asr.engine, config.mt.engine) != (
            "silero", "morph", "haas-onnx", "hy-mt2") or config.recording.default_on:
        raise ValueError("this witness requires the shipped model selection and recording off")
    old = read_fixture("01")
    prefix = bytes(FRAME) + old[2 * 2048:2 * (2048 + 1088)]
    markers = [prefix[FRAME:2 * FRAME], prefix[2 * FRAME:3 * FRAME]]
    speech = {"first": read_fixture("02"), "following": read_fixture("03")}
    if (len(prefix) != PACKET or len(set(markers)) != len(markers)
            or not all(len(m) == FRAME and any(m) and all(sample_offset(p, m) < 0 for p in speech.values())
                       for m in markers)):
        raise ValueError("the prefix requires complete frames unique to the previous lesson")
    vad = SileroVAD(config.vad.threshold)
    decisions = [vad.is_speech(np.frombuffer(prefix[i:i + FRAME], dtype="<i2"))
                 for i in range(0, 3 * FRAME, FRAME)]
    if decisions != [False, True, True]:
        raise ValueError(f"expected two speech frames short of opening, got {decisions}")
    control = await replay(config, b"", markers, speech, action)
    if any(not r["asr"] or not r["delivered"] or r["mt_calls"] < 2 for r in control.values()):
        raise RuntimeError(f"the clean control did not transcribe and translate both fixtures: {control}")
    if any(offset >= 0 for row in control.values() for call in row["asr"] for offset in call["old_frame_offsets"]):
        raise ValueError("previous lesson marker also occurs in an ASR input from the clean control")
    actual = await replay(config, prefix, markers, speech, action)
    prefix_processed_samples = len(prefix) // FRAME * (FRAME // 2)
    offsets = [offset for row in actual.values() for call in row["asr"] for offset in call["old_frame_offsets"]]
    properties = {"old_pcm_absent": all(offset < 0 for offset in offsets)}
    for expected_seq, phase in enumerate(speech, start=1):
        row = actual[phase]
        expected = control[phase]
        properties[f"{phase}_pcm_matches"] = row["asr"] == expected["asr"]
        properties[f"{phase}_captions_match"] = (
            [seq for seq, _ in row["finals"]] == [expected_seq]
            and row["delivered"] and row["mt_calls"] >= 2 and row["captions"] == expected["captions"]
        )
        properties[f"{phase}_timestamps_match"] = (
            len(row["utterances"]) == len(expected["utterances"]) == 1
            and all(timestamp_samples(u[key]) == timestamp_samples(e[key]) + prefix_processed_samples
                    for u, e in zip(row["utterances"], expected["utterances"])
                    for key in ("t_start", "t_end"))
        )
    return {"status": "PASS" if all(properties.values()) else "FAIL", "action": action, "properties": properties,
            "control": control, "after_prefix": actual, "prefix_speech_frames": decisions,
            "prefix_processed_samples": prefix_processed_samples,
            "previous_speech_frames": [
                {"sha256": hashlib.sha256(m).hexdigest(), "head_pcm16": np.frombuffer(m, dtype="<i2")[:8].tolist()}
                for m in markers
            ],
            "pcm_sha256": {name: hashlib.sha256(pcm).hexdigest() for name, pcm in {"prefix": prefix, **speech}.items()},
            "packages": {pkg: version(pkg) for pkg in ("faster-whisper", "onnxruntime", "llama-cpp-python")}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("end", "pause"), default="end")
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    try:
        report = asyncio.run(check(args.models_dir, args.action))
    except Exception as error:
        report = {"status": "ERROR", "error": f"{type(error).__name__}: {error}"}
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    sys.exit({"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]])
