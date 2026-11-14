"""Bounded teacher end-button retries through the real JS and recording server."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import itertools
import json
import logging
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

SYNC_COMMIT = "69d06dfddfd1d36afd6f09e0cf45b4b95de08956"
ROOT = Path(__file__).resolve().parents[2]
BRIDGE = Path(__file__).with_suffix(".cjs")


class Violation(Exception):
    def __init__(self, property, trace, expected, actual):
        self.data = dict(property=property, trace=trace.copy(), expected=expected, actual=actual)


def require(condition, property, trace, expected, actual):
    if not condition:
        raise Violation(property, trace, expected, actual)


class Page:
    def __init__(self, source_root):
        self.process = subprocess.Popen(
            ["node", str(BRIDGE), str(source_root)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        self.lines = queue.Queue()
        def read():
            for line in self.process.stdout:
                self.lines.put(line)
            self.lines.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def call(self, op, **fields):
        self.process.stdin.write(json.dumps(dict(op=op, **fields)) + "\n")
        self.process.stdin.flush()
        try:
            line = self.lines.get(timeout=10)
        except queue.Empty as error:
            raise RuntimeError("teacher page bridge timed out") from error
        if line is None:
            raise RuntimeError(f"teacher page bridge exited: {self.process.stderr.read()}")
        result = json.loads(line)
        if "harness_error" in result:
            raise RuntimeError(result["harness_error"])
        return result

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdout.close()
        self.process.stderr.close()


class Socket:
    def __init__(self):
        self.messages = []
    async def send_json(self, data):
        self.messages.append(data)
    async def close(self, code=1000):
        pass


async def settle():
    for _ in range(12):
        await asyncio.sleep(0)


async def case(page, directory, failure, lost, history):
    from server.asr.fake_engine import FakeASREngine
    from server.config import AppConfig, RecordingConfig, VadConfig
    from server.connection import Connection
    from server.lesson import Lesson
    from server.mt.fake_engine import FakeTranslationEngine
    from server.pipeline import Pipeline
    from server.recorder import SessionRecorder
    from server.roster import Roster
    from server.turn.assembler import Turn

    trace = [f"failure={failure}", "start", "confirmed_turn", "click_end"]
    config = AppConfig(vad=VadConfig(engine="energy", threshold=300))
    config.recording = RecordingConfig(default_on=True, out_dir=str(directory))
    config.turn.strategy = "simple"
    pipeline = Pipeline(Roster(), config, FakeASREngine(), FakeTranslationEngine(["en"]))
    pipeline._tasks = [asyncio.create_task(pipeline._asr_worker())]
    lesson = Lesson(pipeline, config, join_code="4831", teacher_code="604831")
    socket = Socket()
    connection = Connection(socket, "127.0.0.1", lesson, config.limits)
    ui = page.call("reset")

    async def process(snapshot):
        for frame in snapshot["outgoing"]:
            await connection.receive(json.dumps(frame))
        await settle()

    async def deliver():
        nonlocal ui
        await settle()
        messages, socket.messages = socket.messages, []
        for message in messages:
            ui = page.call("message", payload=message)
        return messages

    async def click(button, action):
        nonlocal ui
        ui = page.call("click", id=button)
        require(ui["outgoing"] == [dict(type="control", action=action)],
                "retry_command" if action == "end" else "start_command", trace,
                [dict(type="control", action=action)], ui["outgoing"])
        await process(ui)

    async def reconnect():
        nonlocal ui, socket, connection
        trace.append("disconnect")
        ui = page.call("disconnect")
        require(ui["end_disabled"], "disconnected_controls", trace, True, ui["end_disabled"])
        disconnected = page.call("click", id="end-btn")
        require(not disconnected["outgoing"], "disconnected_controls", trace, [], disconnected["outgoing"])
        if connection._client is not None:
            await lesson.leave(connection._client)
        socket = Socket()
        connection = Connection(socket, "127.0.0.1", lesson, config.limits)
        trace.append("reconnect_joined_ended")
        await process(page.call("reconnect"))
        messages = await deliver()
        require(not ui["end_disabled"], "retry_available", trace, "enabled end button", ui)
        snapshots = [message for message in messages if message.get("type") == "joined"]
        error = snapshots[0].get("recording_save_error") if len(snapshots) == 1 else None
        require(isinstance(error, str) and ui["warning"].endswith(error),
                "save_error_notified", trace, "visible save failure in joined snapshot", messages)

    async def speak(marker):
        turn = Turn(marker, 0, 0.4, 0, time.monotonic(), 1, "strong_end")
        with patch("server.caption_history.datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 14, tzinfo=timezone.utc)
            await pipeline._publish_turn(turn, closed_at=turn.closed_at)
        await deliver()

    def files():
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    try:
        await process(ui)
        await deliver()
        await click("start-btn", "start")
        await deliver()
        await speak("first-lesson")
        recorder = lesson._recorder
        method = "write" if failure == "before_write" else "_write_markdown"
        with patch.object(SessionRecorder, method, side_effect=OSError("disk unavailable")):
            await click("end-btn", "end")
            require(lesson.state == "ended", "unsaved_retained", trace, "ended", lesson.state)
            if lost:
                messages, socket.messages = socket.messages, []
                require(any(msg.get("code") == "recording_save_failed" for msg in messages),
                        "save_error_notified", trace, "recording_save_failed", messages)
                for message in messages:
                    if message.get("code") == "recording_save_failed":
                        break
                    ui = page.call("message", payload=message)
                trace.append("save_failure_notification_lost_at_disconnect")
                await reconnect()
            else:
                messages = await deliver()
                require(any(msg.get("code") == "recording_save_failed" for msg in messages),
                        "save_error_notified", trace, "recording_save_failed", messages)
                require(not ui["end_disabled"], "retry_available", trace, "enabled end button", ui)
            for operation in history:
                trace.append(operation)
                if operation == "disconnect":
                    await reconnect()
                elif operation == "warning":
                    lesson.roster.send_teacher(dict(type="error", code="recording_pending", message="別の警告"))
                    await deliver()
                else:
                    warning = ui["warning"]
                    await click("end-btn", "end")
                    expected = warning if warning.endswith("別の警告") else ""
                    require(ui["warning"] == expected, "warning_ownership", trace, expected, ui["warning"])
                    await deliver()
                    require(not ui["end_disabled"], "retry_available", trace, "enabled end button", ui)
            trace.append("attempt_start_while_save_fails")
            await click("start-btn", "start")
            await deliver()
            require(lesson.state == "ended" and lesson._recorder is recorder,
                    "unsaved_retained", trace, "ended with original recorder", lesson.state)
        destination = recorder._session_dir
        trace.append("repair_disk_then_click_end")
        await click("end-btn", "end")
        await deliver()
        require(destination is None or destination == recorder._session_dir,
                "saved_destination", trace, str(destination), str(recorder._session_dir))
        actual = [json.loads(line)["ja"] for file in directory.rglob("transcript.jsonl")
                  for line in file.read_text(encoding="utf-8").splitlines()]
        markdown = [path.read_text(encoding="utf-8") for path in directory.rglob("transcript.ja.md")]
        require(actual == ["first-lesson"] and recorder._written and
                len(markdown) == 1 and "first-lesson" in markdown[0],
                "saved_complete", trace, "first-lesson in JSONL and Markdown", actual)
        saved = files()
        trace.append("repeat_end_after_success")
        if not ui["end_disabled"]:
            await click("end-btn", "end")
            await deliver()
        require(files() == saved, "saved_immutable", trace, "same files and bytes", sorted(files()))
        trace.append("start_next_lesson")
        await click("start-btn", "start")
        await deliver()
        await speak("second-lesson")
        await click("end-btn", "end")
        await deliver()
        require(all(files().get(name) == content for name, content in saved.items()),
                "saved_immutable", trace, "previous lesson unchanged", sorted(files()))
        recordings = sorted([json.loads(line)["ja"] for line in path.read_text(encoding="utf-8").splitlines()]
                            for path in directory.rglob("transcript.jsonl"))
        require(recordings == [["first-lesson"], ["second-lesson"]],
                "recording_ownership", trace, [["first-lesson"], ["second-lesson"]], recordings)
        require(ui["end_disabled"], "retry_reset", trace, "ordinary ended disables end", ui)
        return None
    except Violation as error:
        return error.data
    finally:
        await lesson.roster.aclose()
        await pipeline.stop()


async def explore(source_root, depth, suite):
    counts, violations = Counter(), []
    page = Page(source_root)
    try:
        with tempfile.TemporaryDirectory(prefix="teacher-recording-retry-") as parent:
            for length in range(depth + 1):
                for history in itertools.product(("retry", "disconnect", "warning"), repeat=length):
                    for failure, lost in itertools.product(("before_write", "after_jsonl"),
                                                          (False,) if suite == "delivered" else (False, True)):
                        with tempfile.TemporaryDirectory(dir=parent) as directory:
                            violation = await asyncio.wait_for(
                                case(page, Path(directory), failure, lost, history), timeout=10)
                        counts["cases"] += 1
                        counts["lost_notification" if lost else "delivered_notification"] += 1
                        counts["failed" if violation else "passed"] += 1
                        if violation:
                            previous = next((item for item in violations if all(
                                item[key] == value for key, value in violation.items())), None)
                            if previous is None:
                                violations.append(dict(**violation, candidate_count=1))
                            else:
                                previous["candidate_count"] += 1
    finally:
        page.close()
    return dict(status="FAIL" if violations else "PASS", counts=dict(counts), violations=violations)


MUTATIONS = [
    ("disable_retry", "web/teacher.js", " && !state.recordingSaveError", "", "retry_available"),
    ("clear_other_warning", "web/teacher.js", "if (el.micStatus.textContent === state.recordingSaveError)", "if (true)", "warning_ownership"),
    ("ignore_recording_snapshot", "web/teacher.js", "applyRecordingSaveError(msg.recording_save_error);", "void msg.recording_save_error;", "retry_available"),
    ("omit_recording_snapshot", "server/lesson.py", "recording_save_error=self._failed_recording_message(),", "recording_save_error=None,", "retry_available"),
    ("allow_disconnected_end", "web/teacher.js", "el.endBtn.disabled = !connected ||", "el.endBtn.disabled =", "disconnected_controls"),
    ("drop_end_command", "web/teacher.js", 'el.endBtn.addEventListener("click", () => sendControl("end"));', 'el.endBtn.addEventListener("click", () => {});', "retry_command"),
    ("skip_unsaved_guard", "server/lesson.py", 'if self.state == "ended" and action != "end":\n                await self._finalize_lesson()', 'if self.state == "ended" and action != "end":\n                pass', "unsaved_retained"),
    ("seal_partial_save", "server/recorder.py", "\n        self._write_markdown(\n", "\n        self._written = final\n        self._write_markdown(\n", "unsaved_retained"),
    ("empty_markdown", "server/recorder.py", 'path.write_text("\\n".join(lines) + "\\n", encoding="utf-8")', 'path.write_text("", encoding="utf-8")', "saved_complete"),
    ("reuse_previous_recorder", "server/lesson.py", "                self._recorder = self._new_recorder()", "                pass", "recording_ownership"),
    ("retain_retry_next_lesson", "web/teacher.js", "    state.recordingSaveError = null;\n  }\n  setPageState(s);", "  }\n  setPageState(s);", "retry_reset"),
    ("allocate_retry_destination", "server/recorder.py", "        if self._session_dir is None:", "        if True:", "saved_destination"),
    ("overwrite_previous_recording", "server/recorder.py", "session_dir.mkdir(exist_ok=False)", "session_dir.mkdir(exist_ok=True)", "saved_immutable"),
    ("mislabel_save_error", "server/lesson.py", 'code="recording_save_failed"', 'code="recording_pending"', "save_error_notified"),
]


def mutation_check(args):
    results = []
    with tempfile.TemporaryDirectory(prefix="teacher-recording-mutations-") as directory:
        scratch = Path(directory)
        for name, relative, before, after, property in MUTATIONS:
            variant = scratch / name
            for folder in ("server", "web"):
                shutil.copytree(args.source_root / folder, variant / folder,
                                ignore=shutil.ignore_patterns("__pycache__"))
            product = variant / relative
            source = product.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"mutation anchor changed: {name}")
            product.write_text(source.replace(before, after), encoding="utf-8")
            output = scratch / f"{name}.json"
            child = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                "--source-root", str(variant), "--suite", "all", "--depth", str(args.depth), "--json", str(output)],
                capture_output=True, text=True, encoding="utf-8", timeout=90)
            report = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
            match = next((item for item in report.get("violations", []) if item["property"] == property), None)
            detected = child.returncode == 1 and report.get("status") == "FAIL" and match is not None
            results.append(dict(mutation=name, property=property, exit_code=child.returncode,
                                detected=detected, counterexample=match))
            if not detected:
                observed = dict(exit_code=child.returncode, status=report.get("status"),
                    properties=sorted({item["property"] for item in report.get("violations", [])}),
                    error=report.get("error"), stderr=child.stderr[:1000])
                raise RuntimeError(f"mutation escaped or harness failed: {name}: {observed}")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--depth", type=int, choices=range(4), default=3)
    parser.add_argument("--suite", choices=("all", "delivered"), default="all")
    parser.add_argument("--mutation-check", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root.resolve()))
    logging.disable(logging.CRITICAL)
    node_version = None
    try:
        node_version = subprocess.check_output(["node", "--version"], text=True, timeout=10).strip()
        report = asyncio.run(explore(args.source_root, args.depth, args.suite))
        if args.mutation_check:
            control = asyncio.run(explore(args.source_root, args.depth, "all"))
            if control["status"] != "PASS":
                raise RuntimeError(f"all-notification mutation control failed: {control}")
            report["mutations"] = dict(control="PASS", results=mutation_check(args))
            restored = asyncio.run(explore(args.source_root, args.depth, args.suite))
            report["mutations"]["restored"] = restored["status"]
            report["mutations"]["restored_matches"] = (
                restored["counts"] == report["counts"] and restored["violations"] == report["violations"])
            if not report["mutations"]["restored_matches"]:
                raise RuntimeError("restored result differs from baseline")
        code = int(report["status"] == "FAIL")
    except Exception as error:
        report = dict(status="ERROR", error=f"{type(error).__name__}: {error}")
        code = 2
    report.update(sync_commit=SYNC_COMMIT, scope=dict(depth=args.depth, suite=args.suite,
        python=sys.version.split()[0], node=node_version,
        operations=["retry", "disconnect", "warning"], failures=["before_write", "after_jsonl"],
        notification=["delivered", "lost_at_disconnect"], source_root=str(args.source_root.resolve())))
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        try:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(output + "\n", encoding="utf-8")
        except OSError as error:
            report.update(status="ERROR", output_error=str(error))
            output = json.dumps(report, ensure_ascii=False, indent=2)
            code = 2
    print(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
