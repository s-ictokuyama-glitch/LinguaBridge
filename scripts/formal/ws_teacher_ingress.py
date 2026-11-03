"""Exhaustively check bounded teacher authorization traces through Connection."""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import Literal
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server import connection
from server.lesson import Lesson
from server.pipeline import Pipeline
from server.roster import Roster
from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, TurnConfig, VadConfig
from server.mt.fake_engine import FakeTranslationEngine

JOIN_CODE = "4831"
TEACHER_CODE = "604831"
JOIN_EVENTS = tuple(itertools.product(("teacher", "student"), (True, False)))
MAX_JOINS = 3
SYNC_COMMIT = "8c6228b15d1db0230529f097eb9a498be0be95f6"


class Wire:
    def __init__(self, connection_type, lesson, limits, *, hold_close=False):
        self.closing = asyncio.Event()
        self.finish_close = asyncio.Event()
        if not hold_close:
            self.finish_close.set()
        self.connection = connection_type(self, "127.0.0.1", lesson, limits)

    async def send_json(self, payload):
        pass

    async def close(self, code=1000):
        self.closing.set()
        await self.finish_close.wait()

    async def exchange(self, frame):
        await self.connection.receive(frame)


def text_frame(**payload):
    return json.dumps(payload)


def join_frame(role: str, correct_code: bool):
    code = TEACHER_CODE if role == "teacher" else JOIN_CODE
    return text_frame(type="join", role=role, lang="en", code=code if correct_code else "0000")


def classroom():
    config = AppConfig(vad=VadConfig(engine="energy", threshold=300),
                       turn=TurnConfig(strategy="simple"))
    pipeline = Pipeline(Roster(config.limits), config, FakeASREngine(), FakeTranslationEngine())
    return Lesson(pipeline, config, join_code=JOIN_CODE, teacher_code=TEACHER_CODE), config.limits


Position = Literal["unjoined", "student", "teacher", "replaced_teacher"]


async def check_frames(wire: Wire, lesson: Lesson, audio_limit: int, position: Position, trace: list[str]) -> dict:
    pipeline = lesson.pipeline
    audio = b"\x01" * audio_limit
    probes = [
        ("audio_at_limit", audio, call.feed_audio(audio)),
        ("audio_over_limit", audio + b"\x00", None),
        *((f"control_{action}", text_frame(type="control", action=action),
           call.control(action)) for action in ("start", "pause", "end")),
        *((f"recording_{on}", text_frame(type="recording", on=on),
           call.set_recording(on)) for on in (True, False)),
    ]
    effects = Mock()
    with (
        patch.object(pipeline, "feed_audio") as feed_audio,
        patch.object(lesson, "control") as control,
        patch.object(lesson, "set_recording") as set_recording,
    ):
        effects.attach_mock(feed_audio, "feed_audio")
        effects.attach_mock(control, "control")
        effects.attach_mock(set_recording, "set_recording")
        for name, frame, expected_call in probes:
            expected = [expected_call] if position == "teacher" and expected_call is not None else []
            await wire.exchange(frame)
            actual = effects.mock_calls
            if actual != expected:
                expected_methods = [effect[0] for effect in expected]
                actual_methods = [effect[0] for effect in actual]
                return dict(trace=trace, position=position, probe=name,
                            expected_effects=expected_methods, actual_effects=actual_methods,
                            arguments_differ=expected_methods == actual_methods)
            effects.reset_mock()
    return {}


async def check_join_history(connection_type, joins: tuple) -> dict:
    lesson, limits = classroom()
    wire = Wire(connection_type, lesson, limits)
    position: Position = "unjoined"
    trace = []
    try:
        for role, correct_code in joins:
            await wire.exchange(join_frame(role, correct_code))
            trace.append(f"join {role} ({'correct' if correct_code else 'wrong'} code)")
            if correct_code:
                position = role
        return await check_frames(wire, lesson, limits.max_audio_bytes, position, trace)
    finally:
        await lesson.pipeline.stop()


async def check_takeover(connection_type) -> dict:
    lesson, limits = classroom()
    old = Wire(connection_type, lesson, limits, hold_close=True)
    new = Wire(connection_type, lesson, limits)
    joining = None
    try:
        await old.exchange(join_frame("teacher", True))
        joining = asyncio.create_task(new.exchange(join_frame("teacher", True)))
        await old.closing.wait()
        trace = ["old joins as teacher", "new joins as teacher", "old close has started"]
        violation = await check_frames(old, lesson, limits.max_audio_bytes, "replaced_teacher", trace)
        old.finish_close.set()
        await joining
        if violation:
            return violation
        return await check_frames(new, lesson, limits.max_audio_bytes, "teacher",
                                  [*trace, "new join completed"])
    finally:
        old.finish_close.set()
        if joining is not None:
            await asyncio.gather(joining, return_exceptions=True)
        await lesson.pipeline.stop()


async def check(connection_type) -> dict:
    checked = 0
    for length in range(MAX_JOINS + 1):
        for joins in itertools.product(JOIN_EVENTS, repeat=length):
            async with asyncio.timeout(3):
                violation = await check_join_history(connection_type, joins)
            if violation:
                return dict(status="FAIL", violation=violation, complete_scenarios=checked)
            checked += 1
    async with asyncio.timeout(3):
        violation = await check_takeover(connection_type)
    if violation:
        return dict(status="FAIL", violation=violation, complete_scenarios=checked)
    return dict(status="PASS", complete_scenarios=checked + 1)


MUTATIONS = {
    "student_audio": (
        "elif self._is_teacher(self._current_client()):",
        "elif self._current_client() is not None:",
        "student", "audio_at_limit", "feed_audio",
    ),
    "replaced_audio": (
        "elif self._is_teacher(self._current_client()):",
        "elif self._is_teacher(self._client):",
        "replaced_teacher", "audio_at_limit", "feed_audio",
    ),
    "replaced_control": (
        "client = self._current_client()",
        "client = self._client",
        "replaced_teacher", "control_start", "control",
    ),
    "student_control": (
        "elif self._is_teacher(client):",
        "elif self._is_teacher(client) or isinstance(message, proto.ControlMessage):",
        "student", "control_start", "control",
    ),
    "student_recording": (
        "elif self._is_teacher(client):",
        "elif self._is_teacher(client) or isinstance(message, proto.RecordingMessage):",
        "student", "recording_True", "set_recording",
    ),
    "audio_limit": (
        "len(frame) > self._limits.max_audio_bytes", "False", "teacher", "audio_over_limit", "feed_audio",
    ),
}


async def check_mutations() -> dict:
    source = Path(connection.__file__).read_text(encoding="utf-8")
    results = {}
    with tempfile.TemporaryDirectory(prefix="ws-teacher-ingress-") as scratch:
        path = Path(scratch) / "connection.py"
        for name, (before, after, position, probe, effect) in MUTATIONS.items():
            if source.count(before) != 1:
                raise ValueError(f"Mutation no longer matches exactly once: {name}")
            path.write_text(source.replace(before, after), encoding="utf-8")
            module = {"__name__": "server.connection"}
            exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module)
            result = await check(module["Connection"])
            witness = result.get("violation", {})
            detected = (
                witness.get("position") == position
                and witness.get("probe") == probe
                and witness.get("expected_effects") == []
                and witness.get("actual_effects") == [effect]
            )
            results[name] = dict(detected=detected, **result)
    return results


async def run(mutation_check: bool) -> dict:
    result = await check(connection.Connection)
    if mutation_check and result["status"] == "PASS":
        result["mutations"] = await check_mutations()
        if not all(mutation["detected"] for mutation in result["mutations"].values()):
            result["status"] = "FAIL"
    return dict(sync_commit=SYNC_COMMIT, **result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutation-check", action="store_true")
    args = parser.parse_args()
    logging.getLogger("server.connection").setLevel(logging.ERROR)
    report = asyncio.run(run(args.mutation_check))
    print(json.dumps(report, indent=2))
    sys.exit(report["status"] != "PASS")
