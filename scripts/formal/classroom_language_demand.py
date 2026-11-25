"""Bound live translation work and check each pupil's captions across a change of attendance."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
from itertools import product
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.formal.lesson_liveness import (
    ControlledExecutor, Recognition, Socket, Translation, Violation, callbacks, japanese,
)
from server.config import load_config
from server.connection import Connection
from server.inference import InferenceRunner, InferenceSucceeded
from server.lesson import Lesson
from server.pipeline import Pipeline
from server.roster import Roster
from server.ws_protocol import JoinMessage


PACKET = 3200
FILES = ("03", "02", "07", "05")
CUTS = ("asr", "mt0", "mt1")
LANG = {"E": "en", "Z": "zh"}


@dataclass(frozen=True)
class Case:
    seats: str
    before: str
    after: str
    cut: str

    def languages(self, mask):
        return {LANG[seat] for seat, bit in zip(self.seats, mask, strict=True) if bit == "1"}

    def feasible(self):
        return len(self.languages(self.before)) >= {"asr": 0, "mt0": 1, "mt1": 2}[self.cut]

    def arguments(self):
        return [self.seats, self.before, self.after, self.cut]


def all_cases():
    masks = ["".join(bits) for bits in product("01", repeat=3)]
    for values in product(("EEZ", "ZZE"), masks, masks, CUTS):
        case = Case(*values)
        if case.feasible():
            yield case


def full_class():
    return (Case("E" * 32 + "Z" * 32, "0" * 64, "1" * 64, "asr"),
            Case("E" * 32 + "Z" * 32 + "E" * 32, "1" * 64 + "0" * 32,
                 "0" * 32 + "1" * 64, "mt1"))


def equal(name, expected, actual, trace):
    if actual == expected:
        return
    if isinstance(expected, list) and isinstance(actual, list):
        index = next((i for i, (a, b) in enumerate(zip(expected, actual)) if a != b),
                     min(len(expected), len(actual)))
        trace = [*trace, f"first difference at index {index}"]
        expected = dict(count=len(expected), item=expected[index:index + 1])
        actual = dict(count=len(actual), item=actual[index:index + 1])
    raise Violation(name, expected, actual, trace)


class Attendance:
    def __init__(self, lesson, seats):
        self.lesson = lesson
        self.seats = seats
        self.present = {}
        self.sockets = {}
        self.expected = {}

    async def change(self, mask, phase):
        for seat in list(self.present):
            if mask[seat] == "0":
                _, client = self.present.pop(seat)
                await self.lesson.leave(client)
        for seat, bit in enumerate(mask):
            if bit == "0" or seat in self.present:
                continue
            socket = Socket()
            message = JoinMessage(type="join", role="student", code=self.lesson.join_code,
                                  lang=LANG[self.seats[seat]])
            client = await self.lesson.join(socket, None, message, "127.0.0.1")
            if client is None:
                raise Violation("admission", "joined", socket.frames, [phase, seat])
            key = (seat, phase)
            self.sockets[key] = socket
            self.expected[key] = []
            self.present[seat] = (key, client)
        return {seat: key for seat, (key, _) in self.present.items()}

    def expect(self, members, seq):
        for key in members.values():
            self.expected[key].append(seq)

    def verify(self, texts, translations, trace):
        for key, sequences in self.expected.items():
            lang = LANG[self.seats[key[0]]]
            expected = [(seq, texts[seq - 1], lang, translations[texts[seq - 1], lang]) for seq in sequences]
            actual = [(m["seq"], m["ja"], m["lang"], m["text"]) for m in self.sockets[key].frames
                      if m["type"] == "caption"]
            equal("student_captions", expected, actual, [*trace, f"seat {key[0]} joined at {key[1]}"])

    def received_count(self):
        return sum(frame["type"] == "caption" for socket in self.sockets.values() for frame in socket.frames)


async def start_lesson(lesson, connection, timeout):
    await lesson.pipeline.start()
    await asyncio.wait_for(lesson.pipeline._warmup_task, timeout)
    if not lesson.pipeline.ready:
        raise RuntimeError("model warmup failed")
    await connection.receive(json.dumps(dict(type="join", role="teacher", code=lesson.teacher_code)))
    await connection.receive('{"type":"control","action":"start"}')


async def exercise(config, case, models=None):
    native = models is not None
    timeout = 120 if native else 5
    if native:
        from scripts.formal.lesson_audio_boundary_real import read_fixture
        from server.main import create_app
        from server.publication import Publication

        config.models.dir = models
        app = create_app(config, Publication(config.server))
        lesson, pipeline = app.state.lesson, app.state.pipeline
        waves = []
        for file in FILES:
            pcm = read_fixture(file) + bytes(12 * PACKET)
            # Align both the 100 ms packets and 32 ms Silero frames.
            waves.append(pcm + bytes(-len(pcm) % 25600))
    else:
        config.vad.engine, config.vad.threshold = "energy", 300
        pipeline = Pipeline(Roster(config.limits), config, Recognition(), Translation())
        lesson = Lesson(pipeline, config, join_code="0412")
        waves = [(1000 + seq).to_bytes(2, "little") * 3200 + bytes(10 * PACKET) for seq in range(1, 5)]

    reached, release = asyncio.Event(), asyncio.Event()
    recognized, requested, translated = [], [], {}
    asr_attempts = 0
    trace = case.arguments()

    def observe(runner, kind):
        run = runner.run

        async def record(operation, *, timeout):
            nonlocal asr_attempts
            if kind == "asr":
                asr_attempts += 1
            else:
                requested.append(operation.args)
            outcome = await run(operation, timeout=timeout)
            if isinstance(outcome, InferenceSucceeded):
                if kind == "asr":
                    recognized.append(outcome.value.text)
                    hold = case.cut == "asr" and len(recognized) == 1
                else:
                    ja, lang = operation.args
                    translated[ja, lang] = outcome.value
                    hold = (case.cut, len(requested)) in (("mt0", 1), ("mt1", 2))
                if hold:
                    reached.set()
                    await release.wait()
            return outcome
        return patch.object(runner, "run", record)

    teacher = Socket()
    connection = Connection(teacher, "127.0.0.1", lesson, config.limits)
    students = Attendance(lesson, case.seats)

    async def feed(pcm):
        for i in range(0, len(pcm), PACKET):
            await connection.receive(pcm[i:i + PACKET])

    async def deliver():
        sends = [sender._queue.join() for sender in pipeline.roster._senders.values()]
        await asyncio.wait_for(asyncio.gather(*sends), timeout)

    async def drain():
        await pipeline.drain(timeout=timeout)
        await deliver()

    async def wait_for_cut():
        # If there was no recognition or translation, drain can win. Report the
        # missing result immediately instead of accepting an unexercised cut.
        gate = asyncio.create_task(reached.wait())
        idle = asyncio.create_task(pipeline.drain(timeout=timeout))
        try:
            done, _ = await asyncio.wait((gate, idle), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise TimeoutError("neither the model result nor pipeline drain completed")
            if idle in done:
                await idle
            equal("model_checkpoint", True, reached.is_set(), trace)
        finally:
            for task in (gate, idle):
                task.cancel()
            await asyncio.gather(gate, idle, return_exceptions=True)

    def verify(demand):
        count = len(demand)
        equal("asr_calls", count, asr_attempts, trace)
        if native:
            texts = recognized
            # Each recording has one distinctive word. Also catch swapped input.
            words = ("教科書", "光合成", "質問", "実験")[:count]
            equal("recording_words", list(words), [w for w, t in zip(words, texts) if w in t], trace)
        else:
            texts = [japanese(i) for i in range(1, count + 1)]
            equal("recognition", texts, recognized, trace)
        finals = [(m["seq"], m["ja"]) for m in teacher.frames if m["type"] == "asr_final"]
        equal("teacher_finals", list(enumerate(texts, 1)), finals, trace)
        expected_calls = [(ja, lang) for ja, langs in zip(texts, demand, strict=True) for lang in langs]
        equal("translations_per_language", sorted(expected_calls), sorted(requested), trace)
        equal("model_results", sorted(expected_calls), sorted(k for k, v in translated.items() if v.strip()), trace)
        translations = translated if native else {(ja, lang): f"{lang}:{ja}" for ja, lang in expected_calls}
        students.verify(texts, translations, trace)

    try:
        await start_lesson(lesson, connection, timeout)
        before = await students.change(case.before, "initial")
        with observe(pipeline._asr_runner, "asr"), observe(pipeline.captions._mt_runner, "mt"):
            await feed(waves[0])
            await wait_for_cut()
            await deliver()
            after = await students.change(case.after, "change")
            demand = [case.languages(case.after if case.cut == "asr" else case.before)]
            if case.cut == "asr":
                students.expect(after, 1)
            else:
                # mt1 changes attendance after one language was sent. The other
                # language still goes only to surviving original connections.
                sent_lang = requested[0][1] if case.cut == "mt1" else None
                students.expect({s: key for s, key in before.items()
                                 if LANG[case.seats[s]] == sent_lang or s in after}, 1)
            release.set()
            await drain()
            verify(demand)

            await feed(waves[1])
            students.expect(after, 2)
            demand.append(case.languages(case.after))
            await drain()
            verify(demand)

            returned = await students.change(case.before, "return")
            for seq in (3, 4):
                students.expect(returned, seq)
                demand.append(case.languages(case.before))
                await feed(waves[seq - 1])
            await drain()
            verify(demand)
        return dict(asr_calls=asr_attempts, mt_calls=len(requested),
                    captions=students.received_count(), texts=recognized)
    finally:
        release.set()
        await pipeline.stop()


async def sustained(config):
    config.vad.engine, config.vad.threshold = "energy", 300
    seats = "E" * 32 + "Z" * 32
    asr, mt = ControlledExecutor(), ControlledExecutor()
    pipeline = Pipeline(Roster(config.limits), config, Recognition(), Translation(),
                        asr_runner=InferenceRunner(asr), mt_runner=InferenceRunner(mt))
    lesson = Lesson(pipeline, config, join_code="0412")
    students = Attendance(lesson, seats)
    teacher = Socket()
    connection = Connection(teacher, "127.0.0.1", lesson, config.limits)
    # 60 utterances per five-minute interval. A fresh join asks only for live captions.
    groups = ("", "E", "EZ", "Z", "EZ", "E", "", "Z", "EZ", "EZ")
    recognized, translated = [], []
    texts, expected_translations = [], {}
    quiet = bytes(PACKET)
    try:
        await start_lesson(lesson, connection, 5)
        for interval, group in enumerate(groups, 1):
            mask = "".join("1" if seat in group else "0" for seat in seats)
            present = await students.change(mask, interval)
            for _ in range(60):
                seq = len(texts) + 1
                ja = japanese(seq)
                texts.append(ja)
                students.expect(present, seq)
                for language in group:
                    lang = LANG[language]
                    expected_translations[ja, lang] = f"{lang}:{ja}"
                voice = (1000 + seq).to_bytes(2, "little") * (PACKET // 2)
                for tick in range(50):
                    await connection.receive(voice if tick < 2 else quiet)
                    if tick % 5 == 4:
                        if asr.current:
                            recognized.append(asr.complete())
                        if mt.current:
                            translated.append(mt.complete())
                    await callbacks()
            trace = [f"{interval * 5} minutes", group, len(texts)]
            equal("asr_calls", list(range(1, len(texts) + 1)), recognized, trace)
            equal("translations_per_language", sorted(expected_translations), sorted(translated), trace)
            finals = [(m["seq"], m["ja"]) for m in teacher.frames if m["type"] == "asr_final"]
            equal("teacher_finals", list(enumerate(texts, 1)), finals, trace)
            students.verify(texts, expected_translations, trace)
        return dict(minutes=50, packets=30000, asr_calls=len(recognized), mt_calls=len(translated),
                    captions=students.received_count())
    finally:
        await pipeline.stop()


async def search(cases, models, *, long_lesson=False):
    config = load_config(ROOT / "config.yaml")
    if ((config.turn.strategy, config.partial.enabled, config.limits.max_students) != ("morph", False, 64)
            or set(config.language_codes) != {"en", "zh"}):
        raise ValueError("review the class size, languages and turn boundaries in config.yaml")
    if models is not None and (config.vad.engine, config.asr.engine, config.mt.engine) != (
            "silero", "haas-onnx", "hy-mt2"):
        raise ValueError("the native replay requires the shipping models")
    results, failures = [], {}
    passed = failed = 0
    by_cut = Counter()
    for case in cases:
        by_cut[case.cut] += 1
        try:
            result = await asyncio.wait_for(exercise(config.model_copy(deep=True), case, models),
                                            timeout=600 if models is not None else 15)
            if len(case.seats) >= 64:
                results.append(dict(cut=case.cut, **result))
            passed += 1
        except Violation as error:
            failed += 1
            failures.setdefault(error.detail["property"], error.detail)
    report = dict(status="FAIL" if failed else "PASS", passed=passed, failed=failed, by_cut=dict(by_cut),
                  full_class=results, first_counterexamples=failures)
    if long_lesson:
        try:
            report["lesson"] = await asyncio.wait_for(sustained(config), 90)
        except Violation as error:
            report.update(status="FAIL", lesson_counterexample=error.detail)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--case", nargs=4, metavar=("SEATS", "BEFORE", "AFTER", "CUT"))
    mode.add_argument("--real", action="store_true")
    mode.add_argument("--lesson", action="store_true", help="only the 50-minute virtual lesson")
    mode.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--models-dir", type=Path)
    args = parser.parse_args()
    if args.models_dir and not args.real:
        parser.error("--models-dir requires --real")
    cases = [*all_cases(), *full_class()]
    models = None
    if args.lesson:
        cases = []
    if args.case:
        case = Case(*args.case)
        if (not case.seats or set(case.seats) - LANG.keys() or case.cut not in CUTS
                or any(len(m) != len(case.seats) or set(m) - {"0", "1"} or m.count("1") > 64
                       for m in (case.before, case.after)) or not case.feasible()):
            parser.error("expected E/Z seats, matching attendance masks, and a reachable asr/mt0/mt1 cut")
        cases = [case]
    if args.real:
        cases = full_class()
        models = str(args.models_dir.resolve() if args.models_dir
                     else load_config(ROOT / "config.yaml").models.resolved_dir)
    report = {}
    try:
        report = asyncio.run(search(cases, models, long_lesson=not args.real and not args.case))
        if args.mutation_check:
            from scripts.formal.classroom_language_mutations import check_mutations
            mutations = check_mutations()
            if report["status"] == "PASS" and not all(m["detected"] for m in mutations.values()):
                raise RuntimeError(f"scratch controls disagreed with the search: {mutations}")
            if asyncio.run(search(cases, models, long_lesson=True)) != report:
                raise RuntimeError("restored search differs from the original")
            report.update(mutations=mutations, restored_matches=True)
    except Exception as error:
        report.update(status="ERROR", error=f"{type(error).__name__}: {error}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return {"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
