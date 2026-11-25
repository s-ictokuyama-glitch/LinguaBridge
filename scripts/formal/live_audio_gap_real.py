"""Compare live Japanese speech with and without a real no-packet interval."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
from unittest.mock import patch

from lesson_audio_boundary_real import PACKET, ROOT, read_fixture
from lesson_liveness import Socket, Violation
from live_audio_gap import join_lesson, messages, require
from server.config import load_config
from server.inference import InferenceSucceeded
from server.main import create_app
from server.publication import Publication


async def replay(config, recordings, interrupted):
    app = create_app(config, Publication(config.server))
    lesson, pipeline = app.state.lesson, app.state.pipeline
    teacher = Socket()
    students = {lang: Socket() for lang in ("en", "zh")}
    asr, mt = pipeline._asr_runner, pipeline.captions._mt_runner
    recognize, translate = asr.run, mt.run
    audio = []
    translations = {lang: [] for lang in students}
    checkpoints = []
    gap_seconds = 0.0
    trace = ["start", "send first 1.0 s of 02.wav"]

    async def observe_audio(operation, *, timeout):
        pcm, rate = operation.args
        require("asr_rate", 16000, rate, trace)
        audio.append(hashlib.sha256(pcm.tobytes()).hexdigest())
        return await recognize(operation, timeout=timeout)

    async def observe_translation(operation, *, timeout):
        source, lang = operation.args
        outcome = await translate(operation, timeout=timeout)
        if isinstance(outcome, InferenceSucceeded):
            translations[lang].append((source, outcome.value))
        return outcome

    def errors():
        return [f["code"] for f in teacher.frames if f["type"] == "error"]

    async def feed(pcm):
        for start in range(0, len(pcm), PACKET):
            await wire.receive(pcm[start:start + PACKET])

    async def delivered():
        queues = [sender._queue.join() for sender in pipeline.roster._senders.values()]
        await asyncio.wait_for(asyncio.gather(*queues), timeout=10)

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(pipeline._warmup_task, timeout=180)
        if not pipeline.ready:
            raise RuntimeError("shipped models did not warm up")
        wire = await join_lesson(lesson, config, teacher, students)
        with patch.object(asr, "run", observe_audio), patch.object(mt, "run", observe_translation):
            for index, pcm in enumerate(recordings, 1):
                if index == 1:
                    await feed(pcm[:10 * PACKET])
                    await delivered()
                    if not pipeline._segmenter.is_open or not pipeline.speaking or audio:
                        raise RuntimeError("02.wav must leave speech open before its first ASR call at 1.0 s")
                    if interrupted:
                        trace.append("omit all PCM until the 30 s microphone warning")
                        require("before_gap_errors", [], errors(), trace)
                        started = asyncio.get_running_loop().time()
                        await asyncio.sleep(config.monitoring.silence_warning_s)
                        try:
                            async with asyncio.timeout(2 * config.monitoring.stats_interval_s + 2):
                                while "mic_silent" not in errors():
                                    await asyncio.sleep(0.05)
                        except TimeoutError:
                            raise Violation("gap_warning", ["mic_silent"], errors(), trace) from None
                        gap_seconds = round(asyncio.get_running_loop().time() - started, 3)
                        await delivered()
                        require("still_live", "live", lesson.state, trace)
                        for lang, socket in students.items():
                            changes = [f["on"] for f in socket.frames if f["type"] == "speaking"]
                            require(f"gap_speaking_{lang}", [True, False], changes, trace)
                    pcm = pcm[10 * PACKET:]
                trace.append(f"complete recording {index} and send quiet PCM")
                await feed(pcm)
                await pipeline.drain(timeout=180)
                await delivered()
                finals, captions = messages(teacher, students)
                require("final_sequences", list(range(1, index + 1)), [seq for seq, _ in finals], trace)
                for lang in students:
                    outputs = translations[lang]
                    require(f"model_sources_{lang}", [ja for _, ja in finals], [ja for ja, _ in outputs], trace)
                    require(f"nonempty_{lang}", True, all(ja.strip() and text.strip() for ja, text in outputs), trace)
                    require(f"captions_{lang}", [(seq, ja, lang, text)
                            for seq, (ja, text) in enumerate(outputs, 1)], captions[lang], trace)
                    if interrupted and index == 1:
                        changes = [f["on"] for f in students[lang].frames if f["type"] == "speaking"]
                        require(f"resumed_speaking_{lang}", [True, False], changes[2:4], trace)
                checkpoints.append(len(finals))
            require("lesson_state", "live", lesson.state, trace)
            require("input_drops", 0, pipeline.asr_dropped_segments, trace)
            require("notices", ["mic_silent"] if interrupted else [], errors(), trace)
            require("connections", [None] * 3, [s.closed for s in (teacher, *students.values())], trace)
    return dict(asr_pcm=audio, finals=finals, captions=captions, checkpoints=checkpoints, gap_seconds=gap_seconds)


async def check(models):
    config = load_config(ROOT / "config.yaml")
    if models is not None:
        config.models.dir = str(models.resolve())
    if (config.vad.engine, config.asr.engine, config.mt.engine, config.turn.strategy,
            config.monitoring.silence_warning_s, config.monitoring.stats_interval_s) != (
            "silero", "haas-onnx", "hy-mt2", "morph", 30, 2):
        raise ValueError("review the real-model and monitor configuration")
    recordings = []
    for name in ("02", "03"):
        pcm = read_fixture(name)
        # An aligned end makes the next recording start on a packet and Silero frame boundary.
        pcm += bytes(16 * PACKET)
        recordings.append(pcm + bytes(-len(pcm) % (8 * PACKET)))
    control = await replay(config, recordings, False)
    resumed = await replay(config, recordings, True)
    require("identical_asr_audio", control["asr_pcm"], resumed["asr_pcm"], ["compare uninterrupted and resumed"])
    require("same_finals", control["finals"], resumed["finals"], ["compare uninterrupted and resumed"])
    return dict(status="PASS", control=control, resumed=resumed,
                input_sha256=[hashlib.sha256(pcm).hexdigest() for pcm in recordings],
                versions={p: version(p) for p in ("faster-whisper", "onnxruntime", "llama-cpp-python")})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    try:
        report = asyncio.run(check(args.models_dir))
    except Violation as error:
        report = dict(status="FAIL", counterexample=error.detail)
    except Exception as error:
        report = dict(status="ERROR", error=f"{type(error).__name__}: {error}")
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    sys.exit({"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]])
