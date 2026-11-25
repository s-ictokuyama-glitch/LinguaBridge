"""Hold one real MT result while distinct Japanese recordings continue to arrive."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
from unittest.mock import patch

from lesson_audio_boundary_real import ROOT, read_fixture
from scripts.formal.lesson_liveness import Socket, Violation
from server.config import load_config
from server.inference import InferenceSucceeded
from server.inference_process import ProcessInferenceRunner
from server.main import create_app
from server.publication import Publication


FILES = ("03", "02", "01", "07", "05")
LANGUAGES = ("en", "zh")


async def classroom(config, recordings, *, congested):
    pipeline = create_app(config, Publication(config.server)).state.pipeline
    teacher = Socket()
    students = {lang: Socket() for lang in LANGUAGES}
    mt = pipeline.captions._mt_runner
    if not all(isinstance(runner, ProcessInferenceRunner) for runner in (mt, pipeline._asr_runner)):
        raise ValueError("expected process-backed recognition and translation")
    run_model = mt.run
    asr = pipeline._asr_runner
    transcribe = asr.run
    asr_seconds = []
    first_result, release = asyncio.Event(), asyncio.Event()
    translations = {lang: [] for lang in LANGUAGES}
    steps = []

    async def observe_asr(operation, *, timeout):
        pcm, rate = operation.args
        asr_seconds.append(len(pcm) / rate)
        return await transcribe(operation, timeout=timeout)

    async def slow_translation(operation, *, timeout):
        outcome = await run_model(operation, timeout=timeout)
        if isinstance(outcome, InferenceSucceeded):
            ja, lang = operation.args
            translations[lang].append((ja, outcome.value))
            if congested and not first_result.is_set():
                first_result.set()
                await release.wait()
        return outcome

    async def feed(file):
        steps.append(f"feed {file}.wav")
        pcm = recordings[file]
        # End each recording on both a 512-sample Silero frame and a 100 ms
        # network packet, with >= 1.2 s quiet. No per-utterance flush or pause.
        pcm += bytes(38400)
        pcm += bytes(-len(pcm) % 25600)
        for offset in range(0, len(pcm), 3200):
            await pipeline.feed_audio(pcm[offset:offset + 3200])

    async def drain():
        await pipeline.drain(timeout=90)
        await asyncio.wait_for(asyncio.gather(*(s._queue.join() for s in pipeline.roster._senders.values())), 10)

    def finals():
        return [(m["seq"], m["ja"]) for m in teacher.frames if m["type"] == "asr_final"]

    def equal(name, expected, actual):
        if expected != actual:
            raise Violation(name, expected, actual, steps)

    try:
        await pipeline.roster.add(teacher, "teacher")
        for lang, socket in students.items():
            await pipeline.roster.add(socket, "student", lang)
        await pipeline.start()
        await asyncio.wait_for(pipeline._warmup_task, 180)
        if not pipeline.ready:
            raise RuntimeError("model warmup failed")
        await pipeline.set_accepting_audio(True)
        by_file = {}
        pressure = {}
        with patch.object(mt, "run", slow_translation), patch.object(asr, "run", observe_asr):
            for file in FILES:
                start = len(finals())
                previous_asr = len(asr_seconds)
                await asyncio.wait_for(feed(file), timeout=10)
                if not congested:
                    await drain()
                    by_file[file] = dict(finals=[ja for _, ja in finals()[start:]],
                                         seconds=asr_seconds[previous_asr:])
                elif file == FILES[0]:
                    await asyncio.wait_for(first_result.wait(), timeout=90)
                elif file == FILES[1]:
                    # Wait until ASR has confirmed B and is blocked in publish.
                    async with asyncio.timeout(60):
                        while len(finals()) < 2:
                            await asyncio.sleep(0.01)
                    equal("full_mt_queue", 1, pipeline.captions.queue_depth)
                    equal("charged_segment", True, pipeline.asr_active_seconds > 0)
                elif file == FILES[3]:
                    pressure = dict(active_s=pipeline.asr_active_seconds, waiting_s=pipeline.asr_wait_seconds,
                                    dropped_s=pipeline.asr_dropped_seconds, dropped=pipeline.asr_dropped_segments)
                    equal("one_drop_while_blocked", 1, pressure["dropped"])
                    equal("accepted_audio_still_waiting", True, pressure["waiting_s"] > 0)
                    steps.append("release MT")
                    release.set()
                    await drain()
            await drain()
        confirmed = finals()
        equal("seq", list(range(1, len(confirmed) + 1)), [seq for seq, _ in confirmed])
        captions = {}
        for lang, socket in students.items():
            # Read the actual text returned for this language and source. A
            # second greedy decode with a different history can word it differently.
            equal("translated_sources_" + lang, [ja for _, ja in confirmed], [ja for ja, _ in translations[lang]])
            if any(not text.strip() for _, text in translations[lang]):
                raise Violation("empty_translation", lang, translations[lang], steps)
            expected = [(seq, ja, lang, text) for seq, (ja, text) in enumerate(translations[lang], 1)]
            captions[lang] = [(m["seq"], m["ja"], m["lang"], m["text"])
                              for m in socket.frames if m["type"] == "caption"]
            equal("captions_" + lang, expected, captions[lang])
        notices = sorted({f["code"] for f in teacher.frames if f["type"] == "error"})
        if congested:
            equal("overload_notices", ["audio_dropped", "mt_backlog"], notices)
        equal("total_drops", int(congested), pipeline.asr_dropped_segments)
        equal("drained", (0, 0), (pipeline.queue_depth, pipeline.audio_queue_seconds))
        equal("connections", [None] * 3, [s.closed for s in (teacher, *students.values())])
        return dict(by_file=by_file, finals=confirmed, captions=captions, pressure=pressure, notices=notices)
    finally:
        release.set()
        await pipeline.stop()


async def check(models_dir):
    config = load_config(ROOT / "config.yaml")
    if models_dir:
        config.models.dir = str(models_dir.resolve())
    if (config.vad.engine, config.asr.engine, config.mt.engine, config.turn.strategy) != (
            "silero", "haas-onnx", "hy-mt2", "morph"):
        raise ValueError("expected shipped Silero, Haas ONNX, Hy-MT2 and morph")
    config.limits.mt_queue_max = config.limits.mt_replay_watermark = 1
    config.limits.asr_queue_seconds = 9
    config.mt.cache_size = 0
    recordings = {file: read_fixture(file) for file in FILES}
    control = await classroom(config, recordings, congested=False)
    rows = control["by_file"]
    for file, row in rows.items():
        if len(row["seconds"]) != 1 or len(row["finals"]) != 1 or not row["finals"][0].strip():
            raise ValueError(f"{file}.wav must produce one segment and one sentence in the control: {row}")
    if len({row["finals"][0] for row in rows.values()}) != len(FILES):
        raise ValueError(f"control sentences must distinguish all five recordings: {rows}")
    active, waiting, dropped = (rows[file]["seconds"][0] for file in FILES[1:4])
    if not active + waiting <= config.limits.asr_queue_seconds < active + waiting + dropped:
        raise ValueError(f"control audio does not cross the intended ASR capacity: {rows}")
    actual = await classroom(config, recordings, congested=True)
    expected_pressure = dict(active_s=active, waiting_s=waiting, dropped_s=dropped, dropped=1)
    if actual["pressure"] != expected_pressure:
        raise Violation("pressure_seconds", expected_pressure, actual["pressure"], FILES)
    expected = [rows[f]["finals"][0] for f in FILES if f != FILES[3]]
    received = [ja for _, ja in actual["finals"]]
    if received != expected:
        raise Violation("admitted_recordings", expected, received, FILES)
    return dict(status="PASS", control=control, congested=actual,
                input_pcm_sha256={k: hashlib.sha256(v).hexdigest() for k, v in recordings.items()},
                versions={p: version(p) for p in ("onnxruntime", "llama-cpp-python", "faster-whisper")})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    try:
        result = asyncio.run(check(args.models_dir))
    except Violation as finding:
        result = dict(status="FAIL", counterexample=finding.detail)
    except Exception as error:
        result = dict(status="ERROR", error=f"{type(error).__name__}: {error}")
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    sys.exit({"PASS": 0, "FAIL": 1, "ERROR": 2}[result["status"]])
