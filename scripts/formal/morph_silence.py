"""Check that a brief sound cannot strand a pending classroom caption in silence."""
from __future__ import annotations

import argparse
import asyncio
from itertools import groupby, product
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).parent)]
from lesson_audio_boundary import FRAME, FrameScores
from lesson_liveness import Socket, Translation
from silero_contract import model_scope
from server.asr.base import ASRResult
from server.config import load_config
from server.pipeline import Pipeline
from server.roster import Roster


CLAUSE = "温度が高いので"
ENDING = "体積が増えます。"
FOLLOWING = "次は実験です。"
WORDS = {1000: CLAUSE, 2000: ENDING, 3000: FOLLOWING}
NOISE = 500
CASES = list(product(range(15, 33), range(4)))


class Recognition:
    def __init__(self):
        self.calls = []

    def warmup(self):
        pass

    def transcribe(self, pcm, sample_rate):
        if sample_rate != 16000:
            raise ValueError(f"unexpected ASR rate: {sample_rate}")
        # A short noise may be inside an open segment. Only marked words contribute text.
        markers = (marker for marker, _ in groupby(pcm[pcm != 0]) if marker != NOISE)
        text = "".join(WORDS[marker] for marker in markers)
        self.calls.append(text)
        return ASRResult(text)


async def inspect_case(config, before, burst):
    roster = Roster(config.limits)
    asr = Recognition()
    with model_scope(FrameScores()):
        pipeline = Pipeline(roster, config, asr, Translation())
    teacher = Socket()
    students = {lang: Socket() for lang in ("en", "zh")}
    differences = []
    observed = {}

    async def feed(marker, frames):
        block = np.full(FRAME, marker, dtype="<i2").tobytes()
        for _ in range(frames):
            await pipeline.feed_audio(block)

    async def checkpoint(phase, calls, turns):
        await pipeline.drain(timeout=5)
        sends = [sender._queue.join() for sender in roster._senders.values()]
        await asyncio.wait_for(asyncio.gather(*sends), 5)
        if any(s.closed is not None for s in (teacher, *students.values())):
            raise RuntimeError("fixture socket closed")
        actual = {
            "asr": asr.calls.copy(),
            "teacher": [[f["seq"], f["ja"]] for f in teacher.frames if f["type"] == "asr_final"],
        }
        rows = list(enumerate(turns, 1))
        expected = {"asr": calls, "teacher": [[seq, text] for seq, text in rows]}
        for lang, socket in students.items():
            actual[lang] = [[f["seq"], f["ja"], f["lang"], f["text"]] for f in socket.frames
                            if f["type"] == "caption"]
            expected[lang] = [[seq, text, lang, f"{lang}:{text}"] for seq, text in rows]
        observed[phase] = actual
        for field, wanted in expected.items():
            if actual[field] != wanted:
                differences.append(dict(phase=phase, field=field, expected=wanted, actual=actual[field]))

    try:
        await roster.add(teacher, "teacher")
        for lang, socket in students.items():
            await roster.add(socket, "student", lang)
        await pipeline.start()
        await asyncio.wait_for(pipeline._warmup_task, 5)
        if not pipeline.ready:
            raise RuntimeError("fixture warmup failed")
        await pipeline.set_accepting_audio(True)
        await feed(1000, 3)
        await feed(0, before)
        await checkpoint("before_burst", [CLAUSE] if before >= 16 else [], [CLAUSE] if before == 32 else [])

        await feed(2000 if burst == 3 else NOISE, burst)
        quiet_fed = 0
        if before >= 16 and burst < 3 and before + burst <= 31:
            quiet_fed = 31 - before - burst
            await feed(0, quiet_fed)
            await checkpoint("before_deadline", [CLAUSE], [])
        await feed(0, 32 - quiet_fed)
        calls = [CLAUSE]
        turns = [CLAUSE]
        if burst == 3:
            # At 15 frames both words share one segment; at 32 the first turn has already closed.
            calls = [CLAUSE + ENDING] if before == 15 else [CLAUSE, ENDING]
            turns = [CLAUSE, ENDING] if before == 32 else [CLAUSE + ENDING]
        await checkpoint("after_silence", calls, turns)

        await feed(3000, 3)
        await feed(0, 32)
        await checkpoint("following", [*calls, FOLLOWING], [*turns, FOLLOWING])
    finally:
        await pipeline.stop()
    return dict(status="FAIL" if differences else "PASS", case=[before, burst],
                differences=differences, observed=observed)


async def search(cases):
    config = load_config(ROOT / "config.yaml")
    morph = config.turn.morph
    if not {"en", "zh"}.issubset(config.language_codes):
        raise ValueError("the classroom needs English and Chinese in config.yaml")
    if (config.vad.engine, config.turn.strategy, config.turn.classifier, config.partial.enabled,
            morph.start_speech_ms, morph.check_silence_ms, morph.force_silence_ms) != (
            "silero", "morph", "surface", False, 96, 500, 1000):
        raise ValueError("review the silence and speech bounds against config.yaml")
    failures = []
    first = None
    for before, burst in cases:
        result = await asyncio.wait_for(inspect_case(config, before, burst), timeout=15)
        if len(cases) == 1:
            return result
        if result["status"] == "FAIL":
            failures.append([before, burst])
            if first is None:
                first = result
    return dict(status="FAIL" if failures else "PASS", cases=len(cases),
                passed=len(cases) - len(failures), failed=len(failures),
                failed_cases=failures, first_counterexample=first)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--case", nargs=2, type=int, metavar=("BEFORE", "BURST"))
    group.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    cases = CASES
    if args.case:
        if tuple(args.case) not in CASES:
            parser.error("choose 15..32 frames of initial silence and 0..3 burst frames")
        cases = [tuple(args.case)]
    report = {}
    try:
        report = asyncio.run(search(cases))
        if args.mutation_check:
            from morph_silence_mutations import check_mutations

            mutations = check_mutations()
            if asyncio.run(search(cases)) != report:
                raise RuntimeError("restored search differs from initial search")
            report.update(mutations=mutations, restored_matches=True)
            if report["status"] == "PASS" and any(not result["detected"] for result in mutations.values()):
                raise RuntimeError("mutation control disagrees with passing full search")
    except Exception as error:
        if "status" in report:
            report["search_status"] = report["status"]
        report.update(status="ERROR", error=f"{type(error).__name__}: {error}")
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return {"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
