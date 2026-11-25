"""Enumerate packet interruptions in a live lesson, up to the bilingual recipients."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from itertools import groupby, product
import json
from pathlib import Path
import subprocess
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).parent)]

from lesson_audio_boundary import FrameScores, PACKET
from lesson_endurance import Clock, clock_scope
from lesson_liveness import Socket, Translation, Violation, callbacks
from morph_silence import CLAUSE, ENDING, FOLLOWING, Recognition
from silero_contract import model_scope
from server.config import load_config
from server.connection import Connection
from server.inference import InferenceSucceeded
from server.lesson import Lesson
from server.pipeline import Pipeline
from server.roster import Roster


# Each counted packet contains 100 ms. The three voiced runs start and end
# on complete 32 ms Silero frames. 800 ms of quiet leaves CLAUSE pending.
MARKERS = [1000] * 8 + [0] * 8 + [2000] * 8 + [0] * 16 + [3000] * 8 + [0] * 16
PACKETS = {marker: np.full(PACKET, marker, dtype="<i2").tobytes() for marker in set(MARKERS)}
DURATIONS = (0, 299, 300, 340)
CASES = tuple(product(range(1, 25), DURATIONS, DURATIONS))


class ImmediateRunner:
    async def run(self, operation, *, timeout):
        return InferenceSucceeded(operation(), 0)

    async def aclose(self):
        pass


class ObservedSocket(Socket):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.indicators = []
        self.warnings = []

    async def send_json(self, payload):
        await super().send_json(payload)
        if payload["type"] == "speaking":
            self.indicators.append((self.clock.tick, payload["on"]))
        if payload["type"] == "error":
            self.warnings.append((self.clock.tick, payload["code"]))


class AudioRecognition(Recognition):
    def __init__(self):
        super().__init__()
        self.audio = []
        self.voiced = []

    def transcribe(self, pcm, sample_rate):
        self.audio.append(hashlib.sha256(pcm.tobytes()).hexdigest())
        self.voiced.append(hashlib.sha256(pcm[pcm != 0].tobytes()).hexdigest())
        return super().transcribe(pcm, sample_rate)


async def join_lesson(lesson, config, teacher, students):
    wire = Connection(teacher, "127.0.0.1", lesson, config.limits)
    await wire.receive(json.dumps(dict(type="join", role="teacher", code=lesson.teacher_code)))
    for lang, socket in students.items():
        student = Connection(socket, "127.0.0.1", lesson, config.limits)
        await student.receive(json.dumps(dict(type="join", role="student", code=lesson.join_code, lang=lang)))
    await wire.receive(json.dumps(dict(type="control", action="start")))
    return wire


def messages(teacher, students):
    finals = [(m["seq"], m["ja"]) for m in teacher.frames if m["type"] == "asr_final"]
    captions = {lang: [(m["seq"], m["ja"], m["lang"], m["text"]) for m in socket.frames
                       if m["type"] == "caption"] for lang, socket in students.items()}
    return finals, captions


def timeline(start, cut, first_gap, second_gap):
    return {start + i + (first_gap if i > cut else 0) + (second_gap if i > 44 else 0): i
            for i in range(1, len(MARKERS) + 1)}


def expected_signals(arrivals, polls):
    voice = {t for t, packet in arrivals.items() if MARKERS[packet - 1]}
    # A warning is due on the first poll of each uninterrupted run of polls
    # that saw no voice in the preceding 30 s. Include speech at the poll tick.
    def quiet(tick):
        return tick >= 300 and not any(tick - 300 < heard <= tick for heard in voice)

    warnings = [next(run) for silent, run in groupby(polls, key=quiet) if silent]
    changes = []
    on = False
    for tick in sorted(voice | set(warnings) | {t for t, p in arrivals.items() if p in (30, 54)}):
        # B and C close after the 16th complete quiet frame, in packets 30 and 54.
        active = tick in voice
        if active != on:
            changes.append((tick, active))
            on = active
    return [(tick, "mic_silent") for tick in warnings], changes


def require(name, expected, actual, trace):
    if actual != expected:
        raise Violation(name, expected, actual, trace)


async def cycle(wire, teacher, students, asr, clock, index, case, control_audio):
    arrivals = timeline(clock.tick, *case)
    polls = range((clock.tick // 20 + 1) * 20, max(arrivals) + 1, 20)
    warnings, indicators = expected_signals(arrivals, polls)
    trace = [f"cycle {index + 1}", f"after packet {case[0]} omit {case[1]} ticks",
             f"after packet 44 omit {case[2]} ticks"]
    for socket in (teacher, *students.values()):
        socket.frames.clear()
        socket.indicators.clear()
        socket.warnings.clear()
    asr_start = len(asr.audio)
    # On ties, receive PCM before yielding the event loop to the stats worker.
    for tick in sorted(arrivals.keys() | set(polls)):
        clock.tick = tick
        packet = arrivals.get(tick)
        if packet is not None:
            await wire.receive(PACKETS[MARKERS[packet - 1]])
        await callbacks()
        if packet in (40, 64):
            finals, captions = messages(teacher, students)
            texts = [CLAUSE + ENDING] if packet == 40 else [CLAUSE + ENDING, FOLLOWING]
            rows = list(enumerate(texts, 2 * index + 1))
            checkpoint = [*trace, f"packet {packet}"]
            require("teacher_finals", rows, finals, checkpoint)
            for lang in students:
                require(f"captions_{lang}", [(i, ja, lang, f"{lang}:{ja}") for i, ja in rows],
                        captions[lang], checkpoint)
    require("asr_text", [CLAUSE, ENDING, FOLLOWING], asr.calls[asr_start:], trace)
    expected_voice = [hashlib.sha256(PACKETS[m] * 8).hexdigest() for m in (1000, 2000, 3000)]
    require("voiced_pcm", expected_voice, asr.voiced[asr_start:], trace)
    audio = asr.audio[asr_start:]
    if control_audio is not None:
        require("asr_pcm", control_audio, audio, trace)
    require("warnings", warnings, teacher.warnings, trace)
    for lang, socket in students.items():
        require(f"speaking_{lang}", indicators, socket.indicators, trace)
        require(f"student_warnings_{lang}", [], socket.warnings, trace)
    return dict(audio=audio, warnings=teacher.warnings.copy(), indicators=students["en"].indicators.copy(),
                end_tick=clock.tick, packets=len(arrivals), captions={lang: len(rows) for lang, rows in captions.items()})


async def inspect(config, schedules, controls=()):
    clock = Clock()
    teacher = ObservedSocket(clock)
    students = {lang: ObservedSocket(clock) for lang in ("en", "zh")}
    asr = AudioRecognition()
    roster = Roster(config.limits)
    with model_scope(FrameScores()):
        pipeline = Pipeline(roster, config, asr, Translation(), asr_runner=ImmediateRunner(),
                            mt_runner=ImmediateRunner())
    lesson = Lesson(pipeline, config, join_code="1234", teacher_code="123456")
    results = []
    with clock_scope(clock):
        try:
            await pipeline.start()
            await callbacks()
            if not pipeline.ready:
                raise RuntimeError("warmup did not finish")
            wire = await join_lesson(lesson, config, teacher, students)
            await callbacks()
            for index, case in enumerate(schedules):
                # Only the first cycle starts without preceding quiet for VAD pre-roll.
                reference = controls[min(index, 1)]["audio"] if controls else None
                results.append(await cycle(wire, teacher, students, asr, clock, index, case, reference))
            require("live", "live", lesson.state, [f"tick {clock.tick}"])
            require("connections", [None] * 3, [s.closed for s in (teacher, *students.values())], ["end"])
            return results
        finally:
            await pipeline.stop()


async def search(cases):
    config = load_config(ROOT / "config.yaml")
    contract = (config.vad.engine, config.turn.strategy, config.turn.classifier,
                config.turn.morph.start_speech_ms, config.turn.morph.check_silence_ms,
                config.turn.morph.force_silence_ms, config.partial.enabled, config.partial.speaking_indicator,
                config.monitoring.silence_warning_s, config.monitoring.stats_interval_s)
    if contract != ("silero", "morph", "surface", 96, 500, 1000, False, True, 30, 2):
        raise ValueError(f"review the packet and warning bounds against config.yaml: {contract}")
    uninterrupted = (1, 0, 0)
    controls = await inspect(config, [uninterrupted, uninterrupted])
    failures = 0
    first = {}
    warning_counts = {}
    for case in cases:
        try:
            observation, = await inspect(config, [case], controls)
            count = len(observation["warnings"])
            warning_counts[count] = warning_counts.get(count, 0) + 1
            if len(cases) == 1:
                return dict(status="PASS", case=case, observation=observation)
        except Violation as error:
            failures += 1
            first.setdefault(error.detail["property"], dict(case=case, **error.detail))
    report = dict(status="FAIL" if failures else "PASS", cases=len(cases), failed=failures,
                  first_counterexamples=first, passed_by_warning_count=warning_counts)
    if len(cases) > 1:
        schedules = [uninterrupted] * 425
        for i in range(8):
            schedules[49 + 50 * i] = (4 if i % 2 == 0 else 16, 350, 0)
        try:
            cycles = await inspect(config, schedules, controls)
            report["lesson"] = dict(virtual_seconds=cycles[-1]["end_tick"] / 10,
                                    packets=sum(row["packets"] for row in cycles),
                                    captions={lang: sum(row["captions"][lang] for row in cycles)
                                              for lang in ("en", "zh")},
                                    warnings=sum(len(row["warnings"]) for row in cycles))
        except Violation as error:
            report.update(status="FAIL", lesson_counterexample=error.detail)
    return report


def timed_search(case):
    # The virtual clock stops advancing if the input handler or cleanup hangs.
    # Run the bound under a separate wall-clock deadline.
    command = [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()), "--probe"]
    if case:
        command.extend(["--case", *map(str, case)])
    child = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=90)
    if child.returncode not in (0, 1):
        raise RuntimeError(f"search exited {child.returncode}: {child.stdout} {child.stderr}")
    report = json.loads(child.stdout)
    if report["status"] != ("PASS" if child.returncode == 0 else "FAIL"):
        raise RuntimeError(f"search status disagrees with its exit code: {report}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--case", type=int, nargs=3, metavar=("CUT", "GAP1", "GAP2"))
    group.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    cases = CASES if args.case is None else [tuple(args.case)]
    if args.case and cases[0] not in CASES:
        parser.error("CUT must be 1..24 and both gaps must be 0, 299, 300 or 340 ticks")
    try:
        report = asyncio.run(search(cases)) if args.probe else timed_search(args.case)
        if args.mutation_check:
            from live_audio_gap_mutations import check_mutations

            mutations = check_mutations()
            if report["status"] == "PASS" and not all(item["detected"] for item in mutations.values()):
                raise RuntimeError("a scratch control disagrees with the complete search")
            if timed_search(args.case) != report:
                raise RuntimeError("restored search differs from its initial result")
            report.update(mutations=mutations, restored_matches=True)
    except Violation as error:
        report = dict(status="FAIL", control_failure=error.detail)
    except Exception as error:
        report = dict(status="ERROR", error=f"{type(error).__name__}: {error}")
    output = json.dumps(report, indent=2, ensure_ascii=False)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return {"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
