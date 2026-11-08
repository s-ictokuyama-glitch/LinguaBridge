"""Exhaust finite, single-consumer schedules against the actual AsrBacklog."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SYNC_COMMIT = "5381edb92062d1a35972b3a7dee8d387c5b0aa1b"
OPERATIONS = ("seg1", "seg2", "seg4", "short", "interim", "boundary", "flush",
              "take", "finish", "error", "cancel", "wait_cancel")
SAMPLE_RATE = 16000
UNIT = 8000


class Violation(Exception):
    def __init__(self, property_name, trace, expected, actual):
        self.detail = dict(property=property_name, trace=list(trace),
                           expected=expected, actual=actual)


def require(name, trace, expected, actual):
    if actual != expected:
        raise Violation(name, trace, expected, actual)


async def scenario(operations):
    import numpy as np
    from server.asr_backlog import AsrBacklog, FlushTurn
    from server.audio.vad import InterimSegment, Segment, TurnBoundary

    backlog = AsrBacklog(1.5, sample_rate=SAMPLE_RATE, min_interim_audio_s=1.0)
    waiting = []  # Accepted event identities and sample counts, independently tracked.
    active = None
    worker = None
    release = None
    trace = []
    dropped = skipped = dropped_units = 0

    async def consume(entered, gate):
        async with backlog.next() as event:
            entered.set_result(event)
            await gate

    async def complete(mode):
        nonlocal active, worker
        if mode == "cancel":
            worker.cancel()
        elif mode == "error":
            release.set_exception(RuntimeError("injected ASR failure"))
        else:
            release.set_result(None)
        try:
            await worker
        except asyncio.CancelledError:
            if mode != "cancel":
                raise
        except RuntimeError:
            if mode != "error":
                raise
        worker = None
        active = None

    async def snapshot():
        require("accounting", trace,
                [len(waiting), sum(n for _, n in waiting) / SAMPLE_RATE,
                 (active[1] if active else 0) / SAMPLE_RATE,
                 not waiting and active is None, dropped, dropped_units * .5, skipped],
                [backlog.depth, backlog.waiting_seconds, backlog.active_seconds,
                 backlog.is_idle, backlog.dropped_segments, backlog.dropped_seconds,
                 backlog.interims_skipped])
        require("accounting", trace,
                (sum(n for _, n in waiting) + (active[1] if active else 0)) / SAMPLE_RATE,
                backlog.total_seconds)
        join = asyncio.create_task(backlog.join())
        try:
            # One ready-queue turn lets Queue.join either return or await its event.
            await asyncio.sleep(0)
            require("completion", trace, not waiting and active is None, join.done())
            if join.done():
                join.result()
        finally:
            join.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await join

    try:
        await snapshot()
        for operation in operations:
            # Only legal single-consumer schedules are in the finite domain.
            if operation == "take" and (active is not None or not waiting):
                return False
            if operation in ("finish", "error", "cancel") and active is None:
                return False
            if operation == "wait_cancel" and (waiting or active is not None):
                return False
            trace.append(operation)
            if operation == "wait_cancel":
                entered = asyncio.get_running_loop().create_future()
                release = asyncio.get_running_loop().create_future()
                worker = asyncio.create_task(consume(entered, release))
                await asyncio.sleep(0)
                require("completion", trace, False, entered.done())
                await complete("cancel")
            elif operation == "take":
                entered = asyncio.get_running_loop().create_future()
                release = asyncio.get_running_loop().create_future()
                worker = asyncio.create_task(consume(entered, release))
                # A broken consumer must become an error, not an endless checker run.
                done, _ = await asyncio.wait((worker, entered), timeout=2,
                                             return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise RuntimeError("consumer failed to enter next()")
                if worker in done:
                    worker.result()
                    raise RuntimeError("consumer exited before its gate")
                expected = waiting.pop(0)
                event = entered.result()
                require("fifo", trace, True, event is expected[0])
                active = expected
            elif operation in ("finish", "error", "cancel"):
                await complete(operation)
            else:
                units = int(operation[-1]) if operation.startswith("seg") else 0
                if units:
                    event = Segment(np.zeros(units * UNIT, dtype=np.int16), 0, units * .5, 0)
                    total = sum(n for _, n in waiting) + (active[1] if active else 0)
                    outcome = "dropped" if total and total + units * UNIT > 3 * UNIT else "accepted"
                    property_name = "capacity"
                elif operation in ("short", "interim"):
                    size = UNIT if operation == "short" else 2 * UNIT
                    event = InterimSegment(np.zeros(size, dtype=np.int16), 0, size / SAMPLE_RATE, 0)
                    outcome = "skipped" if waiting or active is not None or size < SAMPLE_RATE else "accepted"
                    property_name = "interim_admission"
                else:
                    event = TurnBoundary(0, 0) if operation == "boundary" else FlushTurn(0)
                    outcome, property_name = "accepted", "fifo"
                require(property_name, trace, outcome, backlog.offer(event).value)
                if outcome == "accepted":
                    waiting.append((event, units * UNIT))
                elif outcome == "dropped":
                    dropped += 1
                    dropped_units += units
                else:
                    skipped += 1
            await snapshot()
        # Fair successful drain is a postcondition, outside the operation-length bound.
        if active is not None:
            trace.append("drain_active")
            await complete("finish")
        while waiting:
            trace.append("drain_next")
            async with backlog.next() as event:
                expected = waiting.pop(0)
                require("fifo", trace, True, event is expected[0])
            await snapshot()
        await snapshot()
        return True
    finally:
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                await worker


async def check(depth, case):
    counts = dict(attempted=0, legal=0, violations=0)
    violations = {}
    sequences = [tuple(case.split(","))] if case else (
        seq for size in range(depth + 1) for seq in itertools.product(OPERATIONS, repeat=size)
    )
    for sequence in sequences:
        if any(op not in OPERATIONS for op in sequence):
            raise ValueError("unknown operation")
        counts["attempted"] += 1
        try:
            counts["legal"] += bool(await scenario(sequence))
        except Violation as error:
            counts["violations"] += 1
            violations.setdefault(error.detail["property"], error.detail)
    return dict(status="FAIL" if violations else "PASS", sync_commit=SYNC_COMMIT,
                depth=depth if not case else None, case=case, counts=counts,
                violations=list(violations.values()))


def mutation_check():
    mutations = (
        ("ignore_active", "capacity", "seg2,take,seg2",
         "total_samples = self._waiting_samples + self._active_samples",
         "total_samples = self._waiting_samples"),
        ("reverse_queue", "fifo", "boundary,flush,take",
         "= asyncio.Queue()", "= asyncio.LifoQueue()"),
        ("leak_active", "accounting", "seg1,take,cancel",
         "self._active_samples -= samples", "self._active_samples -= 0"),
        ("forget_done", "completion", "seg1,take,cancel",
         "self._queue.task_done()", "pass  # broken completion"),
        ("busy_interim", "interim_admission", "boundary,interim",
         "not self.is_idle", "False"),
    )
    results = []
    for name, property_name, case, old, new in mutations:
        healthy = asyncio.run(check(0, case))
        if healthy["status"] != "PASS" or healthy["counts"]["legal"] != 1:
            raise RuntimeError(f"mutation witness is not healthy: {name}")
        with tempfile.TemporaryDirectory(prefix="asr-backlog-formal-") as directory:
            scratch = Path(directory)
            shutil.copytree(ROOT / "server", scratch / "server",
                            ignore=shutil.ignore_patterns("__pycache__"))
            script = scratch / "scripts/formal/asr_backlog.py"
            script.parent.mkdir(parents=True)
            shutil.copy2(__file__, script)
            target = scratch / "server/asr_backlog.py"
            source = target.read_text(encoding="utf-8")
            if source.count(old) != 1:
                raise RuntimeError(f"mutation site changed: {name}")
            target.write_text(source.replace(old, new), encoding="utf-8")
            child = subprocess.run([sys.executable, "-B", str(script), "--case", case],
                                   cwd=scratch, capture_output=True, text=True,
                                   encoding="utf-8", timeout=30)
            if child.returncode != 1:
                raise RuntimeError(f"mutation failed to report a violation: {name}\n{child.stderr}")
            result = json.loads(child.stdout)
            detected = any(v["property"] == property_name for v in result["violations"])
            if not detected:
                raise RuntimeError(f"mutation missed {property_name}: {name}")
            results.append(dict(name=name, detected=detected, healthy="PASS",
                                violations=result["violations"]))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depth", type=int, default=4, choices=range(0, 7))
    parser.add_argument("--case", help="one comma-separated schedule")
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    result = asyncio.run(check(args.depth, args.case))
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
