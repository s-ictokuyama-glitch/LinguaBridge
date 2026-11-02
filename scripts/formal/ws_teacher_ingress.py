"""Exhaustively check bounded teacher authorization traces at the /ws entry point."""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import runpy
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server import main
from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, TurnConfig, VadConfig
from server.mt.fake_engine import FakeTranslationEngine

JOIN_CODE = "4831"
JOIN_EVENTS = tuple(itertools.product(("teacher", "student"), (True, False)))
MAX_JOINS = 3
SYNC_COMMIT = "0194bc1b0973cc6ab1fdb16def022d78f2e6a9fa"


class Wire:
    def __init__(self, app, *, hold_close: bool = False) -> None:
        self.incoming: asyncio.Queue[dict] = asyncio.Queue()
        self.ready = asyncio.Event()
        self.closing = asyncio.Event()
        self.finish_close = asyncio.Event()
        if not hold_close:
            self.finish_close.set()
        self.incoming.put_nowait({"type": "websocket.connect"})
        scope = dict(
            type="websocket", asgi={"version": "3.0"}, http_version="1.1", scheme="ws",
            path="/ws", raw_path=b"/ws", root_path="", query_string=b"", headers=[],
            client=("127.0.0.1", 12345), server=("127.0.0.1", 8000), subprotocols=[],
        )
        self.task = asyncio.create_task(app(scope, self.receive, self.send))

    async def receive(self) -> dict:
        if self.incoming.empty():
            self.ready.set()
        return await self.incoming.get()

    async def send(self, message: dict) -> None:
        if message["type"] == "websocket.close":
            self.closing.set()
            await self.finish_close.wait()

    async def wait_for(self, event: asyncio.Event) -> None:
        waiter = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait((waiter, self.task), return_when=asyncio.FIRST_COMPLETED)
            if self.task in done:
                self.task.result()
                raise RuntimeError("WebSocket handler returned before the expected checkpoint")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    def submit(self, frame: dict) -> None:
        self.ready.clear()
        self.incoming.put_nowait({"type": "websocket.receive", **frame})

    async def exchange(self, frame: dict) -> None:
        self.submit(frame)
        await self.wait_for(self.ready)

    async def close(self) -> None:
        self.finish_close.set()
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, timeout=3)


def text_frame(**payload) -> dict:
    return {"text": json.dumps(payload)}


def join_frame(role: str, correct_code: bool) -> dict:
    return text_frame(type="join", role=role, lang="en", code=JOIN_CODE if correct_code else "0000")


def new_app(create_app):
    config = AppConfig(vad=VadConfig(engine="energy", threshold=300),
                       turn=TurnConfig(strategy="simple"))
    app = create_app(config, None, asr_engine=FakeASREngine(), mt_engine=FakeTranslationEngine(),
                     join_code=JOIN_CODE)
    return app, config.limits.max_audio_bytes


Position = Literal["unjoined", "student", "teacher", "replaced_teacher"]


async def check_frames(wire: Wire, app, audio_limit: int, position: Position, trace: list[str]) -> dict:
    lesson = app.state.lesson
    pipeline = app.state.pipeline
    audio = b"\x01" * audio_limit
    probes = [
        ("audio_at_limit", {"bytes": audio}, call.feed_audio(audio)),
        ("audio_over_limit", {"bytes": audio + b"\x00"}, None),
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


async def check_join_history(create_app, joins: tuple) -> dict:
    app, limit = new_app(create_app)
    wire = Wire(app)
    position: Position = "unjoined"
    trace = []
    try:
        await wire.wait_for(wire.ready)
        for role, correct_code in joins:
            await wire.exchange(join_frame(role, correct_code))
            trace.append(f"join {role} ({'correct' if correct_code else 'wrong'} code)")
            if correct_code:
                position = role
        return await check_frames(wire, app, limit, position, trace)
    finally:
        await wire.close()


async def check_takeover(create_app) -> dict:
    app, limit = new_app(create_app)
    async with AsyncExitStack() as stack:
        old = Wire(app, hold_close=True)
        new = Wire(app)
        stack.push_async_callback(new.close)
        stack.push_async_callback(old.close)
        await old.wait_for(old.ready)
        await new.wait_for(new.ready)
        await old.exchange(join_frame("teacher", True))
        new.submit(join_frame("teacher", True))
        await old.wait_for(old.closing)
        trace = ["old joins as teacher", "new joins as teacher", "old close has started"]
        violation = await check_frames(old, app, limit, "replaced_teacher", trace)
        old.finish_close.set()
        await new.wait_for(new.ready)
        if violation:
            return violation
        return await check_frames(new, app, limit, "teacher", [*trace, "new join completed"])


async def check(create_app) -> dict:
    checked = 0
    for length in range(MAX_JOINS + 1):
        for joins in itertools.product(JOIN_EVENTS, repeat=length):
            async with asyncio.timeout(3):
                violation = await check_join_history(create_app, joins)
            if violation:
                return dict(status="FAIL", violation=violation, complete_scenarios=checked)
            checked += 1
    async with asyncio.timeout(3):
        violation = await check_takeover(create_app)
    if violation:
        return dict(status="FAIL", violation=violation, complete_scenarios=checked)
    return dict(status="PASS", complete_scenarios=checked + 1)


MUTATIONS = {
    "student_audio": (
        ' and client.role == "teacher":', ':', "student", "audio_at_limit", "feed_audio",
    ),
    "replaced_audio": (
        " and roster.contains(client)", "", "replaced_teacher", "audio_at_limit", "feed_audio",
    ),
    "replaced_control": (
        " or not roster.contains(client)", "", "replaced_teacher", "control_start", "control",
    ),
    "student_control": (
        'if client.role == "teacher":\n                        await lesson.control',
        'if client is not None:\n                        await lesson.control', "student", "control_start", "control",
    ),
    "student_recording": (
        'if client.role == "teacher":\n                        lesson.set_recording',
        'if client is not None:\n                        lesson.set_recording', "student", "recording_True", "set_recording",
    ),
    "audio_limit": (
        "len(data) > config.limits.max_audio_bytes", "False", "teacher", "audio_over_limit", "feed_audio",
    ),
}


async def check_mutations() -> dict:
    source = Path(main.__file__).read_text(encoding="utf-8")
    results = {}
    with tempfile.TemporaryDirectory(prefix="ws-teacher-ingress-") as scratch:
        path = Path(scratch) / "main.py"
        for name, (before, after, position, probe, effect) in MUTATIONS.items():
            if source.count(before) != 1:
                raise ValueError(f"Mutation no longer matches exactly once: {name}")
            path.write_text(source.replace(before, after), encoding="utf-8")
            module = runpy.run_path(str(path), run_name=main.__name__)
            create_app = module["create_app"]
            create_app.__globals__["WEB_DIR"] = main.WEB_DIR
            result = await check(create_app)
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
    result = await check(main.create_app)
    if mutation_check and result["status"] == "PASS":
        result["mutations"] = await check_mutations()
        if not all(mutation["detected"] for mutation in result["mutations"].values()):
            result["status"] = "FAIL"
    return dict(sync_commit=SYNC_COMMIT, **result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutation-check", action="store_true")
    args = parser.parse_args()
    logging.getLogger("server.main").setLevel(logging.ERROR)
    report = asyncio.run(run(args.mutation_check))
    print(json.dumps(report, indent=2))
    sys.exit(report["status"] != "PASS")
