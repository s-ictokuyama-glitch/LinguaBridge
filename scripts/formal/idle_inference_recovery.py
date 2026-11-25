"""Check the first bilingual captions after an idle inference child exits."""
from __future__ import annotations

import argparse
import asyncio
from itertools import product
import json
import logging
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.formal.classroom_language_demand import start_lesson
from scripts.formal.lesson_liveness import Recognition, Socket, Translation, japanese
from server.config import load_config
from server.connection import Connection
from server.inference import InferenceSucceeded
from server.inference_process import ProcessInferenceRunner
from server.lesson import Lesson
from server.pipeline import Pipeline
from server.roster import Roster

FAULTS = {"none": (), "asr": ("asr",), "mt": ("mt",), "both": ("asr", "mt")}
FILES = {"A": ("03", "教科書"), "B": ("02", "光合成"), "C": ("07", "質問"), "D": ("05", "実験")}
PACKET = 3200


def make_lesson(config, native):
    if native:
        from server.main import create_app
        from server.publication import Publication

        return create_app(config, Publication(config.server)).state.lesson
    config.vad.engine, config.vad.threshold = "energy", 300
    asr, mt = Recognition(), Translation()
    startup = config.limits.inference_startup_timeout_s
    pipeline = Pipeline(
        Roster(config.limits), config, asr, mt,
        asr_runner=ProcessInferenceRunner(asr, Recognition, startup_timeout_s=startup, name="asr"),
        mt_runner=ProcessInferenceRunner(mt, Translation, startup_timeout_s=startup, name="mt"),
    )
    return Lesson(pipeline, config, join_code="0412")


def waves(native):
    if native:
        from scripts.formal.lesson_audio_boundary_real import read_fixture

        result = {key: read_fixture(file) for key, (file, _) in FILES.items()}
    else:
        result = {key: (1000 + i).to_bytes(2, "little") * 3200 for i, key in enumerate(FILES, 1)}
    # Pad to both the 100 ms packet grid and the 32 ms Silero frame grid.
    return {key: pcm + bytes(-len(pcm) % 25600 + 25600 * 2) for key, pcm in result.items()}


async def exit_while_idle(runner):
    generation = runner._generation
    if (generation is None or generation.startup is None or not generation.startup.done()
            or generation.cleanup is not None
            or (generation.request is not None and not generation.request.done())):
        raise RuntimeError("fault injection needs a completed inference on a prepared generation")
    child = generation.process
    if not child.is_alive():
        raise RuntimeError("inference child exited before injection")
    # This handle belongs to the child started by this case. join establishes
    # that it is already dead before sending any PCM for the next utterance.
    child.kill()
    await asyncio.to_thread(child.join, 10)
    if child.exitcode is None:
        raise RuntimeError("idle child did not exit")


def received(teacher, students):
    return {
        "teacher": [[m["seq"], m["ja"]] for m in teacher.frames if m["type"] == "asr_final"],
        **{lang: [[m["seq"], m["ja"], m["lang"], m["text"]] for m in socket.frames
                  if m["type"] == "caption"] for lang, socket in students.items()},
    }


def identify(ja, native):
    for i, (key, (_, word)) in enumerate(FILES.items(), 1):
        matches = word in ja if native else ja == japanese(i)
        if matches:
            return key
    return f"unknown({ja})"


def observations(frames, native, model_results):
    """Keep the input identity and sequence; retain the actual payload on a mismatch."""
    finals = dict(frames["teacher"])
    observed = {}
    for recipient, messages in frames.items():
        rows = []
        for seq, ja, *caption in messages:
            key = identify(ja, native)
            row = [seq, key]
            if recipient != "teacher":
                lang, text = caption
                expected = model_results.get((ja, recipient)) if native else f"{recipient}:{ja}"
                if lang != recipient or finals.get(seq) != ja or not text.strip() or text != expected:
                    row.append(dict(ja=ja, language=lang, text=text, expected_text=expected))
            rows.append(row)
        observed[recipient] = rows
    return observed


async def run_case(fault, next_word, cache, native=False, models=None):
    config = load_config(ROOT / "config.yaml")
    if (config.vad.engine, config.asr.engine, config.turn.strategy, config.mt.engine) != (
            "silero", "haas-onnx", "morph", "hy-mt2") or config.mt.cache_size <= 0:
        raise ValueError("review the shipping model, turn and cache settings")
    if cache == "off":
        config.mt.cache_size = 0
    if models:
        config.models.dir = str(models.resolve())
    audio = waves(native)
    lesson = make_lesson(config, native)
    pipeline = lesson.pipeline
    runners = {"asr": pipeline._asr_runner, "mt": pipeline.captions._mt_runner}
    teacher = Socket()
    connection = Connection(teacher, "127.0.0.1", lesson, config.limits)
    students = {lang: Socket() for lang in ("en", "zh")}
    timeout = 180 if native else 15
    model_results = {}
    translate = runners["mt"].run

    async def record_translation(operation, *, timeout):
        outcome = await translate(operation, timeout=timeout)
        if isinstance(outcome, InferenceSucceeded):
            model_results[operation.args] = outcome.value
        return outcome

    fed = []

    async def feed_and_drain(words):
        for key in words:
            for start in range(0, len(audio[key]), PACKET):
                await connection.receive(audio[key][start:start + PACKET])
            fed.append(key)
        await pipeline.drain(timeout=timeout)
        sends = [sender._queue.join() for sender in lesson.roster._senders.values()]
        await asyncio.wait_for(asyncio.gather(*sends), timeout)
        return observations(received(teacher, students), native, model_results)

    violations = {}

    def compare(phase, words, actual):
        expected = [[i, key] for i, key in enumerate(words, 1)]
        for recipient, rows in actual.items():
            if expected != rows:
                violations[f"{phase}.{recipient}"] = dict(expected=expected, actual=rows)

    words = "A" + next_word + "CD"
    exited = []
    try:
        await start_lesson(lesson, connection, timeout)
        for lang, socket in students.items():
            pupil = Connection(socket, "127.0.0.1", lesson, config.limits)
            await pupil.receive(json.dumps(dict(type="join", role="student", code=lesson.join_code, lang=lang)))
        with patch.object(runners["mt"], "run", record_translation):
            actual = await feed_and_drain("A")
            compare("before_exit", "A", actual)
            if not violations:
                for kind in FAULTS[fault]:
                    await exit_while_idle(runners[kind])
                    exited.append(kind)
                actual = await feed_and_drain(next_word)
                compare("next", words[:2], actual)
                # Both later utterances arrive without an intervening drain.
                actual = await feed_and_drain("CD")
                compare("end", words, actual)
                last = actual["teacher"][-1:]
                if (not last or last[0][1:] != ["D"]
                        or any(actual[lang][-1:] != last for lang in students)):
                    violations["following_bilingual"] = dict(expected="D on all three receivers",
                                                             actual={k: v[-1:] for k, v in actual.items()})
        result = dict(case=[fault, next_word, cache], exited=exited, pcm="".join(fed),
                      violations=violations, received=actual)
        if native:
            result["frames"] = received(teacher, students)
        return result
    finally:
        try:
            await lesson.shutdown()
        finally:
            await pipeline.stop()


async def suite(cases, native=False, models=None):
    results = [await run_case(*case, native=native, models=models) for case in cases]
    failed = sum(bool(row["violations"]) for row in results)
    return dict(status="FAIL" if failed else "PASS", passed=len(results) - failed, failed=failed, cases=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--case", nargs=3, metavar=("EXIT", "NEXT", "CACHE"))
    mode.add_argument("--real", action="store_true")
    mode.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    cases = list(product(FAULTS, ("A", "B"), ("on", "off")))
    if args.case and tuple(args.case) not in cases:
        parser.error("--case expects none/asr/mt/both, A/B and on/off")
    if args.models_dir and not args.real:
        parser.error("--models-dir requires --real")
    try:
        if args.real:
            cases = [(fault, "B", "on") for fault in FAULTS]
        elif args.case:
            cases = [args.case]
        report = asyncio.run(suite(cases, args.real, args.models_dir))
        if args.mutation_check:
            from scripts.formal.idle_inference_mutations import check_mutations

            mutations = check_mutations()
            if asyncio.run(suite(cases)) != report:
                raise RuntimeError("full search did not reproduce the original results after mutations")
            report.update(mutations=mutations, restored_matches=True)
    except Exception as error:
        report = dict(status="ERROR", error=f"{type(error).__name__}: {error}")
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
        summary = {key: value for key, value in report.items() if key not in ("cases", "mutations")}
        if "mutations" in report:
            summary["detected_mutations"] = list(report["mutations"])
        print(json.dumps(dict(summary, report=str(args.json)), ensure_ascii=False))
    else:
        print(output)
    return {"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]]


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    sys.exit(main())
