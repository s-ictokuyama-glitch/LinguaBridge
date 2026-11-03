"""Bounded replay recovery schedules against the real asyncio implementation."""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import Executor, Future
import itertools
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
SYNC_COMMIT = "f4f77289ca4a69b1c401cb39b4f93e5e70cb51a4"
OPERATIONS = ("set_en", "set_zh", "live_turn", "disconnect", "rejoin")
WITNESSES = ("retarget", "isolation", "language", "admission")


class FirstTranslationGate(Executor):
    def __init__(self):
        self.started = asyncio.Event()
        self.held = None
        self.released = False

    def submit(self, fn, /, *args, **kwargs):
        future = Future()
        future.set_running_or_notify_cancel()
        if self.released:
            future.set_result(fn(*args, **kwargs))
        else:
            if self.held is not None:
                raise AssertionError("single MT worker submitted two held jobs")
            self.held = (future, fn, args, kwargs)
            self.started.set()
        return future

    def release(self):
        self.released = True
        future, fn, args, kwargs = self.held
        future.set_result(fn(*args, **kwargs))


class Socket:
    def __init__(self):
        self.sent = []
        self.closed = None

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000):
        self.closed = code


async def scenario(operations, watermark, *, admission=False):
    from server import ws_protocol as proto
    from server.asr.fake_engine import FakeASREngine
    from server.config import AppConfig
    from server.inference import InferenceRunner
    from server.lesson import Lesson
    from server.mt.fake_engine import FakeTranslationEngine
    from server.pipeline import Pipeline
    from server.roster import Roster
    from server.turn.assembler import Turn

    config = AppConfig.model_validate({
        "vad": {"engine": "energy", "threshold": 300}, "turn": {"strategy": "simple"},
        "mt": {"cache_size": 0}, "recording": {"default_on": False},
        "limits": {"mt_queue_max": 8, "mt_replay_watermark": watermark},
    })
    roster = Roster(config.limits)
    gate = FirstTranslationGate()
    pipeline = Pipeline(
        roster, config, FakeASREngine(), FakeTranslationEngine(), mt_runner=InferenceRunner(gate),
    )
    lesson = Lesson(pipeline, config, join_code="1234")
    trace = []
    peers = []
    client = None
    socket = None
    owed = []
    serial = 0

    async def publish():
        nonlocal serial
        serial += 1
        turn = Turn(f"turn-{serial}", 0, 1, 0, time.monotonic(), 1, "strong_end")
        utterance = pipeline.captions.confirm(turn)
        await pipeline.captions.publish(utterance, closed_at=turn.closed_at)

    async def join():
        nonlocal client, socket, owed
        socket = Socket()
        owed = list(range(1, serial + 1))
        client = await lesson.join(socket, None, proto.JoinMessage(
            type="join", role="student", lang="en", code="1234", last_seq=0,
        ), "formal-client")

    try:
        await lesson.control("start")
        await publish()
        if admission:
            for _ in range(watermark):
                await publish()
        for lang in ("en", "zh"):
            peer_socket = Socket()
            await roster.add(peer_socket, "student", lang)
            peers.append(peer_socket)
        trace.extend([f"retained_history({serial})", "rejoin(last_seq=0,en)"])
        await join()
        if not admission:
            pipeline.captions.start()
            await asyncio.wait_for(gate.started.wait(), timeout=2)
            trace.append("MT_started(seq=1,en)")
            for operation in operations:
                trace.append(operation)
                if operation.startswith("set_"):
                    if client is not None:
                        await lesson.set_lang(client, operation[4:])
                elif operation == "live_turn":
                    await publish()
                elif operation == "disconnect":
                    if client is not None:
                        await roster.remove(client)
                        client = None
                elif operation == "rejoin" and (client is None or not roster.contains(client)):
                    await join()
            gate.release()
            trace.append("MT_complete_then_drain")
            await pipeline.drain(timeout=2)
        else:
            gate.released = True
            pipeline.captions.start()
            trace.append("drain_after_admission")
            await pipeline.drain(timeout=2)

        active = client is not None and roster.contains(client)
        await roster.aclose()
        violations = []
        captions = [m for m in socket.sent if m.get("type") == "caption"]
        received = sorted({m["seq"] for m in captions})
        if len(received) != len(captions):
            violations.append({"property": "replay_duplicate", "expected": received,
                               "actual": [m["seq"] for m in captions]})
        if client is not None and socket.closed != 1013:
            missing = sorted(set(owed) - set(received))
            if missing:
                violations.append({
                    "property": "replay_recoverable", "missing": missing,
                    "expected": {"received": owed, "or_close_code": 1013},
                    "actual": {"received": received, "close_code": socket.closed,
                               "client_present_before_cleanup": active},
                })
        leaked = [m for peer in peers for m in peer.sent
                  if m.get("type") == "caption" and m["seq"] == 1]
        if leaked:
            violations.append({"property": "replay_isolation", "expected": [], "actual": leaked})
        wrong = [m for m in captions if active and m["lang"] != client.lang]
        if wrong:
            violations.append({"property": "replay_language", "expected": client.lang, "actual": wrong})
        for violation in violations:
            violation.update({"trace": trace, "watermark": watermark})
        screen = {
            "language": client.lang if client is not None else None,
            "frames": socket.sent,
            "owed": owed if active and socket.closed != 1013 else [],
            "trace": trace,
            "watermark": watermark,
        }
        return violations, screen
    finally:
        await pipeline.stop()


def check_screens(screens):
    command = ["node", str(ROOT / "scripts/formal/caption_recovery_view.cjs")]
    rendered = subprocess.run(
        command, input=json.dumps(screens), capture_output=True,
        encoding="utf-8", check=True, timeout=30,
    )
    return json.loads(rendered.stdout)


def screen_violation(screen, rendered):
    cards = {}
    expected_last_seq = 0
    for frame in screen["frames"]:
        if frame["type"] == "joined":
            expected_last_seq = max(expected_last_seq, frame["history_from"] - 1)
        if frame["type"] == "caption" and frame["lang"] == screen["language"]:
            cards.setdefault(frame["seq"], frame)
    while expected_last_seq + 1 in cards:
        expected_last_seq += 1
    expected = {"cards": [cards[seq] for seq in sorted(cards)], "last_seq": expected_last_seq}
    if rendered != expected or rendered["last_seq"] < max(screen["owed"], default=0):
        return {
            "property": "screen_recovery", "trace": screen["trace"],
            "watermark": screen["watermark"], "expected": expected,
            "required_replay": screen["owed"], "actual": rendered,
        }
    return None


async def check(args):
    counts = {"schedules": 0, "admission": 0, "witnesses": 0, "violating_scenarios": 0}
    findings = {}
    witnesses = {}
    cases = []

    def record(violations):
        if violations:
            counts["violating_scenarios"] += 1
        for violation in violations:
            key = (violation["property"], violation["watermark"])
            if key not in findings:
                findings[key] = {**violation, "occurrences": 0}
            findings[key]["occurrences"] += 1

    if args.suite == "all":
        for watermark in (1, 2):
            for length in range(args.depth + 1):
                for operations in itertools.product(OPERATIONS, repeat=length):
                    cases.append((*await scenario(operations, watermark), None))
                    counts["schedules"] += 1
            cases.append((*await scenario((), watermark, admission=True), None))
            counts["admission"] += 1
    for name in WITNESSES:
        if args.case is not None and args.case != name:
            continue
        operations = ("set_zh",) if name in ("retarget", "language") else ()
        if name == "retarget":
            operations += ("live_turn",)
        cases.append((*await scenario(operations, 1, admission=name == "admission"), name))
        counts["witnesses"] += 1
    screens = [screen for _, screen, _ in cases]
    for (violations, screen, name), rendered in zip(cases, check_screens(screens), strict=True):
        violation = screen_violation(screen, rendered)
        if violation is not None:
            violations.append(violation)
        record(violations)
        if name is not None:
            witnesses[name] = "FAIL" if violations else "PASS"
    counts["screens"] = len(screens)
    return {
        "status": "FAIL" if findings else "PASS", "sync_commit": SYNC_COMMIT,
        "scope": {"depth": args.depth, "operations": OPERATIONS, "watermarks": [1, 2],
                  "mt_queue_max": 8, "python": sys.version.split()[0]},
        "counts": counts, "violations": list(findings.values()), "witnesses": witnesses,
    }


def mutation_check(args):
    mutations = (
        ("drop_retarget", "retarget", "replay_recoverable",
         'elif selected not in attempted:', 'elif False:'),
        ("broadcast_replay", "isolation", "replay_isolation",
         'self.roster.send(client, captions[selected])',
         'self.roster.send_language(selected, captions[selected])'),
        ("send_old_language", "language", "replay_language",
         'selected = client.lang', 'selected = job.lang'),
        ("duplicate_caption", "isolation", "replay_duplicate",
         'if self.roster.send(client, captions[selected]):',
         'self.roster.send(client, captions[selected])\n'
         '                    if self.roster.send(client, captions[selected]):'),
        ("skip_admission_retry", "admission", "replay_recoverable",
         'await self.roster.retry(client)', 'pass'),
        ("remove_without_retry_close", "admission", "replay_recoverable",
         'await self.roster.retry(client)', 'await self.roster.remove(client)'),
    )
    results = []
    for name, case, property_name, old, new in mutations:
        with tempfile.TemporaryDirectory(prefix="caption-recovery-") as directory:
            source = Path(directory)
            shutil.copytree(args.source_root / "server", source / "server",
                            ignore=shutil.ignore_patterns("__pycache__"))
            path = source / "server" / "caption_delivery.py"
            original = path.read_text(encoding="utf-8")
            if original.count(old) != 1:
                raise RuntimeError(f"mutation anchor changed: {name}")
            report = source / "result.json"
            command = [sys.executable, str(Path(__file__).resolve()), "--suite", "witnesses",
                       "--case", case, "--source-root", str(source), "--json", str(report)]
            baseline = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if baseline.returncode != 0:
                raise RuntimeError(f"baseline witness must pass: {name}\n{baseline.stdout}\n{baseline.stderr}")
            path.write_text(original.replace(old, new), encoding="utf-8")
            shutil.rmtree(source / "server" / "__pycache__", ignore_errors=True)
            broken = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if broken.returncode != 1 or not report.exists():
                raise RuntimeError(f"mutation did not produce a counterexample: {name}\n{broken.stdout}\n{broken.stderr}")
            data = json.loads(report.read_text(encoding="utf-8"))
            violations = [v for v in data["violations"] if v["property"] == property_name]
            if not violations:
                raise RuntimeError(f"mutation missed expected property: {name}")
            results.append({"mutation": name, "baseline_status": "PASS", "detected": True,
                            "counterexample": violations[0]})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depth", type=int, default=4, choices=range(0, 5))
    parser.add_argument("--suite", choices=("all", "witnesses"), default="all")
    parser.add_argument("--case", choices=WITNESSES)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--mutation-check", action="store_true")
    args = parser.parse_args()
    if args.case is not None and args.suite != "witnesses":
        parser.error("--case requires --suite witnesses")
    args.source_root = args.source_root.resolve()
    sys.path.insert(0, str(args.source_root))
    logging.disable(logging.CRITICAL)
    result = asyncio.run(check(args))
    if args.mutation_check:
        result["mutations"] = mutation_check(args)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
