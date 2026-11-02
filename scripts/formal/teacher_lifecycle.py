"""Exhaust bounded teacher handover schedules against Lesson, Roster and Pipeline."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass
import itertools
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SYNC_COMMIT = "2a1c1f68ae2c372e03b0bc1a50820cf793f73586"
SERIAL = ("join_A", "join_B", "leave_A", "leave_B", "start", "pause", "end")
OVERLAP = ("join_C", "leave_C", "start", "pause", "end", "release_A")
PREFIX = ("join_A", "start", "begin_join_B_at_close_A")


class Violation(Exception):
    def __init__(self, name, trace, expected, actual):
        self.detail = dict(property=name, trace=list(trace), expected=expected, actual=actual)


def require(name, trace, expected, actual):
    if expected != actual:
        raise Violation(name, trace, expected, actual)


@dataclass
class Expected:
    state: str = "idle"
    auto_paused: bool = False
    teacher: str | None = None

    def joined(self, name):
        self.teacher = name
        self.complete_join(name)

    def complete_join(self, name):
        if self.teacher == name and self.auto_paused:
            self.state = "live"
            self.auto_paused = False

    def left(self, name):
        if self.teacher == name:
            self.teacher = None
            if self.state == "live":
                self.state = "paused"
                self.auto_paused = True

    def control(self, action):
        self.state = dict(start="live", pause="paused", end="ended")[action]
        self.auto_paused = False


class Socket:
    def __init__(self, name):
        self.name = name
        self.sent = []
        self.closed = []
        self.hold_close = False
        self.close_entered = asyncio.Event()
        self.release_close = asyncio.Event()

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000):
        self.closed.append(code)
        if self.hold_close:
            self.close_entered.set()
            await self.release_close.wait()


async def scenario(operations, *, overlap=False):
    from server import ws_protocol as proto
    from server.asr.fake_engine import FakeASREngine
    from server.config import AppConfig
    from server.lesson import Lesson
    from server.mt.fake_engine import FakeTranslationEngine
    from server.pipeline import Pipeline
    from server.roster import Roster

    config = AppConfig.model_validate({
        "vad": {"engine": "energy", "threshold": 300}, "turn": {"strategy": "simple"},
        "recording": {"default_on": False},
        "limits": {"send_queue_max": 128, "send_timeout_s": 3600},
    })
    roster = Roster(config.limits)
    pipeline = Pipeline(roster, config, FakeASREngine(), FakeTranslationEngine())
    lesson = Lesson(pipeline, config, join_code="1234")
    worker = asyncio.create_task(pipeline._asr_worker())
    expected = Expected()
    clients, sockets = {}, []
    trace = []
    pending = None
    held = None
    joined_c = False

    async def join(name):
        socket = Socket(name)
        sockets.append(socket)
        client = await lesson.join(socket, None, proto.JoinMessage(
            type="join", role="teacher", code="1234",
        ), f"formal-{name}")
        if client is None:
            raise RuntimeError("valid teacher join was rejected")
        clients[name] = client
        return socket

    def inspect():
        owners = [c.ws.name for c in roster.clients if c.role == "teacher"]
        require("teacher_owner", trace, [] if expected.teacher is None else [expected.teacher], owners)
        require("session_state", trace, expected.state, lesson.state)
        require("pause_reason", trace, expected.auto_paused, lesson.auto_paused)
        require("pipeline_state", trace, expected.state, pipeline._state)

    async def release():
        nonlocal pending
        held.release_close.set()
        await asyncio.wait_for(pending, timeout=5)
        pending = None
        expected.complete_join("B")

    try:
        if overlap:
            held = await join("A")
            expected.joined("A")
            await lesson.control("start")
            expected.control("start")
            held.hold_close = True
            pending = asyncio.create_task(join("B"))
            await asyncio.wait_for(held.close_entered.wait(), timeout=5)
            expected.teacher = "B"
            trace.extend(PREFIX)
            inspect()
        for operation in operations:
            if operation in ("start", "pause", "end"):
                if expected.teacher is None or (expected.teacher == "B" and pending is not None):
                    return False
                trace.append(operation)
                await lesson.control(operation)
                expected.control(operation)
            elif operation.startswith("join_"):
                name = operation[-1]
                if overlap and joined_c:
                    return False
                trace.append(operation)
                await join(name)
                expected.joined(name)
                joined_c = joined_c or name == "C"
            elif operation.startswith("leave_"):
                name = operation[-1]
                if name not in clients:
                    return False
                trace.append(operation)
                await lesson.leave(clients[name])
                expected.left(name)
            elif operation == "release_A":
                if pending is None:
                    return False
                trace.append(operation)
                await release()
            else:
                raise ValueError(f"unknown operation: {operation}")
            inspect()
        if pending is not None:
            trace.append("release_A_at_tail")
            await release()
            inspect()
        return True
    finally:
        for socket in sockets:
            socket.release_close.set()
        if pending is not None:
            await asyncio.wait_for(pending, timeout=5)
        await asyncio.wait_for(pipeline.drain(), timeout=5)
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        await roster.aclose()
        pipeline._asr_executor.shutdown(wait=True)
        pipeline._mt_executor.shutdown(wait=True)


async def check(depth, *, case=None, overlap=False):
    counts = dict(attempted=0, legal=0, violating_scenarios=0)
    violations = {}
    suites = ((overlap, [tuple(case.split(","))]),) if case is not None else (
        (False, (seq for size in range(depth + 1) for seq in itertools.product(SERIAL, repeat=size))),
        (True, (seq for size in range(depth + 1) for seq in itertools.product(OVERLAP, repeat=size))),
    )
    for concurrent, sequences in suites:
        alphabet = OVERLAP if concurrent else SERIAL
        for sequence in sequences:
            if any(op not in alphabet for op in sequence):
                raise ValueError("operation does not belong to this suite")
            counts["attempted"] += 1
            try:
                counts["legal"] += bool(await scenario(sequence, overlap=concurrent))
            except Violation as error:
                counts["legal"] += 1
                counts["violating_scenarios"] += 1
                detail = {**error.detail, "suite": "overlap" if concurrent else "serial"}
                key = detail["property"]
                if key not in violations or len(detail["trace"]) < len(violations[key]["trace"]):
                    violations[key] = detail
    return dict(status="FAIL" if violations else "PASS", sync_commit=SYNC_COMMIT,
                depth=depth if case is None else None, case=case,
                counts=counts, violations=list(violations.values()))


def mutation_check():
    mutations = (
        ("keep_old_teacher", "teacher_owner", "join_A,start,join_B", "roster.py",
         'old = self.teacher() if role == "teacher" else None', 'old = None'),
        ("stale_leave_pauses", "session_state", "join_A,start,join_B,leave_A", "lesson.py",
         'self.roster.teacher() is None and self.state == "live"', 'self.state == "live"'),
        ("forget_auto_pause", "pause_reason", "join_A,start,leave_A", "lesson.py",
         'self.auto_paused = True', 'self.auto_paused = False'),
        ("resume_manual_pause", "session_state", "join_A,start,pause,join_B", "lesson.py",
         'if self.auto_paused:', 'if self.auto_paused or self.state == "paused":'),
        ("pause_after_end", "session_state", "join_A,start,end,leave_A", "lesson.py",
         'and self.state == "live":', 'and self.state in ("live", "ended"):'),
        ("forget_auto_resume", "session_state", "join_A,start,leave_A,join_B", "lesson.py",
         'await self._transition("live")', 'await self._transition("paused")'),
        ("pipeline_ignores_state", "pipeline_state", "join_A,start", "pipeline.py",
         'self._state = state', 'self._state = previous'),
    )
    results = []
    for name, property_name, case, filename, old, new in mutations:
        healthy = asyncio.run(check(0, case=case))
        if healthy["status"] != "PASS" or healthy["counts"]["legal"] != 1:
            raise RuntimeError(f"mutation witness is not healthy: {name}")
        with tempfile.TemporaryDirectory(prefix="teacher-lifecycle-formal-") as directory:
            scratch = Path(directory)
            shutil.copytree(ROOT / "server", scratch / "server",
                            ignore=shutil.ignore_patterns("__pycache__"))
            script = scratch / "scripts/formal/teacher_lifecycle.py"
            script.parent.mkdir(parents=True)
            shutil.copy2(__file__, script)
            target = scratch / "server" / filename
            source = target.read_text(encoding="utf-8")
            if source.count(old) != 1:
                raise RuntimeError(f"mutation site changed: {name}")
            target.write_text(source.replace(old, new), encoding="utf-8")
            child = subprocess.run([sys.executable, "-B", str(script), "--case", case],
                                   cwd=scratch, capture_output=True, text=True,
                                   encoding="utf-8", timeout=30)
            if child.returncode != 1:
                raise RuntimeError(f"mutation did not report a violation: {name}\n{child.stderr}")
            result = json.loads(child.stdout)
            if not any(v["property"] == property_name for v in result["violations"]):
                raise RuntimeError(f"mutation missed {property_name}: {name}")
            results.append(dict(name=name, healthy="PASS", detected=True,
                                violations=result["violations"]))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depth", type=int, default=4, choices=range(0, 6))
    parser.add_argument("--case", help="one comma-separated schedule")
    parser.add_argument("--overlap", action="store_true", help="run --case after the held-close prefix")
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if args.overlap and args.case is None:
        parser.error("--overlap requires --case")
    sys.path.insert(0, str(ROOT))
    logging.disable(logging.CRITICAL)
    result = asyncio.run(check(args.depth, case=args.case, overlap=args.overlap))
    if args.mutation_check:
        result["mutations"] = mutation_check()
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output, encoding="utf-8")
    print(output, end="")
    return int(result["status"] == "FAIL")


if __name__ == "__main__":
    raise SystemExit(main())
