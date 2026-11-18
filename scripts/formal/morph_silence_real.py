"""Replay a short VAD interruption between a Japanese clause and its translation."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts/formal")]

from lesson_audio_boundary_real import FRAME, PACKET, read_fixture
from lesson_liveness_real import Socket
from server.config import load_config
from server.main import create_app
from server.publication import Publication


async def replay(config, first, following):
    app = create_app(config, Publication(config.server))
    pipeline = app.state.pipeline
    teacher = Socket()
    students = {lang: Socket() for lang in ("en", "zh")}
    decisions = []
    detector = pipeline._segmenter._vad
    is_speech = detector.is_speech

    def observe(frame):
        speech = is_speech(frame)
        decisions.append(speech)
        return speech

    async def feed(pcm):
        if len(pcm) % PACKET:
            raise ValueError("checkpoints must be at complete 100 ms packets")
        for offset in range(0, len(pcm), PACKET):
            await pipeline.feed_audio(pcm[offset:offset + PACKET])
        await pipeline.drain(timeout=180)
        await asyncio.wait_for(asyncio.gather(*(
            sender._queue.join() for sender in pipeline.roster._senders.values()
        )), timeout=10)

    def received():
        return {
            "teacher": [[m["seq"], m["ja"]] for m in teacher.frames if m["type"] == "asr_final"],
            "students": {
                lang: [[m["seq"], m["ja"], m["lang"], m["text"]] for m in socket.frames
                       if m["type"] == "caption"]
                for lang, socket in students.items()
            },
        }

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(pipeline._warmup_task, timeout=180)
        if not pipeline.ready:
            raise RuntimeError("model warmup failed")
        await pipeline.roster.add(teacher, "teacher")
        for lang, socket in students.items():
            await pipeline.roster.add(socket, "student", lang)
        await app.state.lesson.control("start")
        with patch.object(detector, "is_speech", observe):
            # At 2.6 s the clause's Segment has closed, before the 2.752 s interruption.
            await feed(first[:26 * PACKET])
            if pipeline.asr_calls != 1 or not pipeline._assembler.has_pending or received()["teacher"]:
                raise RuntimeError("fixture did not yield one pending Haas clause before the interruption")
            await feed(first[26 * PACKET:])
            before = received()
            speech_frames = [i for i, speech in enumerate(decisions) if speech]
            frame_count = len(decisions)
            await feed(following)
            after = received()
    return {"before_next": before, "after_next": after, "first_speech_frames": speech_frames,
            "first_frames": frame_count}


def delivered(messages, count):
    finals = messages["teacher"]
    return ([seq for seq, _ in finals] == list(range(1, count + 1))
            and all(text.strip() for _, text in finals)
            and all([m[:2] for m in rows] == finals and all(m[2] == lang and m[3].strip() for m in rows)
                    for lang, rows in messages["students"].items()))


async def check(models):
    config = load_config(ROOT / "config.yaml")
    if models:
        config.models.dir = str(models.resolve())
    if (config.vad.engine, config.turn.strategy, config.turn.classifier,
            config.asr.engine, config.mt.engine) != ("silero", "morph", "surface", "haas-onnx", "hy-mt2"):
        raise ValueError("this fixture needs the shipped Silero, morph, Haas and Hy-MT configuration")
    config.recording.default_on = False
    config.mt.cache_size = 0
    first = read_fixture("01")
    clause_and_gap = first[:64 * FRAME] + bytes(22 * FRAME)
    brief = first[5 * FRAME:6 * FRAME]
    tail = bytes(63 * FRAME)
    control_pcm = clause_and_gap + bytes(FRAME) + tail
    interrupted_pcm = clause_and_gap + brief + tail
    following = read_fixture("03")
    following += bytes(-len(following) % PACKET + 16 * PACKET)
    control = await replay(config, control_pcm, following)
    actual = await replay(config, interrupted_pcm, following)
    if (control["first_frames"] != 150 or actual["first_frames"] != 150
            or control["first_speech_frames"][-1:] != [63]
            or actual["first_speech_frames"] != [*control["first_speech_frames"], 86]):
        raise ValueError("native Silero did not detect the intended single-frame interruption")
    if not delivered(control["before_next"], 1) or not delivered(control["after_next"], 2):
        raise RuntimeError(f"uninterrupted control failed to translate both utterances: {control}")
    properties = {
        phase: delivered(actual[phase], count) and actual[phase]["teacher"] == control[phase]["teacher"]
        for phase, count in (("before_next", 1), ("after_next", 2))
    }
    return {"status": "PASS" if all(properties.values()) else "FAIL", "properties": properties,
            "control": control, "interrupted": actual,
            "input_sha256": {name: hashlib.sha256(pcm).hexdigest() for name, pcm in (
                ("control", control_pcm), ("interrupted", interrupted_pcm), ("following", following))},
            "packages": {pkg: version(pkg) for pkg in ("onnxruntime", "faster-whisper", "llama-cpp-python")}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    try:
        report = asyncio.run(check(args.models_dir))
    except Exception as error:
        report = {"status": "ERROR", "error": f"{type(error).__name__}: {error}"}
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    sys.exit({"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]])
