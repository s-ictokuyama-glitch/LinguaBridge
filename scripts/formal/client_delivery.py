"""Exhaust bounded send/fault/close schedules against the actual ClientSender."""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SYNC_COMMIT = "d78968533639ca29108318dd9e572407f26c1c10"
OPERATIONS = ("send", "send_history", "dispatch", "success", "error", "timeout", "close", "abort")


class Violation(Exception):
    def __init__(self, name, trace, expected, actual):
        self.detail = dict(property=name, trace=list(trace), expected=expected, actual=actual)


def require(name, trace, expected, actual):
    if expected != actual:
        raise Violation(name, trace, expected, actual)


async def pump():
    for _ in range(12):
        await asyncio.sleep(0)


class Socket:
    def __init__(self):
        self.sent = []
        self.closed = []
        self.current = None
        self.gate = None
        self.automatic = False

    async def send_json(self, payload):
        self.current = payload["seq"]
        self.gate = asyncio.get_running_loop().create_future()
        try:
            if not self.automatic:
                await self.gate
            self.sent.append(payload["seq"])
        finally:
            self.current = None
            self.gate = None

    async def close(self, code=1000):
        self.closed.append(code)

    def release(self, outcome="success"):
        if self.gate is None or self.gate.done():
            raise RuntimeError("socket has no pending send")
        if outcome == "success":
            self.gate.set_result(None)
        elif outcome == "error":
            self.gate.set_exception(ConnectionResetError("injected transport failure"))
        else:
            self.gate.set_exception(TimeoutError("injected timeout outcome"))


async def scenario(operations, capacity):
    from server.delivery import ClientSender

    socket = Socket()
    sender = ClientSender(socket, maxsize=capacity, send_timeout_s=3600, close_code=1013)
    queued, sent = [], []
    active = None
    alive = True
    killed = False
    trace = [f"capacity={capacity}"]
    serial = 0

    async def observe():
        require("admission", trace, alive, sender.alive)
        require("fifo", trace, sent, socket.sent)
        if alive:
            require("capacity", trace, len(queued), sender.pending)
        if killed:
            await pump()
        require("retry_close", trace, [1013] if killed else [], socket.closed)

    async def drain():
        nonlocal active, alive
        expected = sent + ([active] if active is not None else []) + queued
        socket.automatic = True
        if socket.gate is not None and not socket.gate.done():
            socket.release()
        await sender.aclose(drain_timeout_s=1)
        require("drain", trace, expected, socket.sent)
        sent[:] = expected
        queued.clear()
        active, alive = None, False

    try:
        for operation in operations:
            if operation in ("success", "error", "timeout") and (not alive or active is None):
                return False
            if operation == "dispatch" and not alive:
                return False
            trace.append(operation)
            if operation in ("send", "send_history"):
                serial += 1
                history = operation == "send_history"
                required_slots = 2 if history else 1
                accepted = alive and capacity - len(queued) >= required_slots
                payload = {"type": "caption", "seq": serial}
                actual = sender.send(payload, drop_if_full=True) if history else sender.send(payload)
                property_name = "history_reservation" if history else "capacity"
                require(property_name if alive else "admission", trace, accepted, actual)
                if accepted:
                    queued.append(serial)
                elif alive and not history:
                    alive, killed = False, True
                if history:
                    require("history_reservation", trace, alive, sender.alive)
            elif operation == "dispatch":
                if active is None and queued:
                    active = queued.pop(0)
                await pump()
                require("fifo", trace, active, socket.current)
            elif operation in ("success", "error", "timeout"):
                socket.release(operation)
                if operation == "success":
                    sent.append(active)
                    active = queued.pop(0) if queued else None
                else:
                    active, alive, killed = None, False, True
                await pump()
                if alive:
                    require("fifo", trace, active, socket.current)
            elif operation == "close":
                if alive:
                    await drain()
                else:
                    await sender.aclose(drain_timeout_s=0)
            else:
                await sender.aclose(drain_timeout_s=0)
                alive, active = False, None
            await observe()
        trace.append("final_drain" if alive else "final_close")
        if alive:
            await drain()
        else:
            await sender.aclose(drain_timeout_s=0)
        await observe()
        require("admission", trace, False, sender.send({"seq": serial + 1}))
        return True
    finally:
        await sender.aclose(drain_timeout_s=0)


async def witness(name):
    from server.delivery import ClientSender
    from server.config import LimitsConfig
    from server.roster import Roster

    trace = [name]
    if name == "real_timeout":
        socket = Socket()
        sender = ClientSender(socket, maxsize=1, send_timeout_s=0, close_code=1013)
        try:
            sender.send({"seq": 1})
            await pump()
            require("timeout", trace, False, sender.alive)
            require("retry_close", trace, [1013], socket.closed)
            require("timeout", trace, [], socket.sent)
            require("admission", trace, False, sender.send({"seq": 2}))
        finally:
            await sender.aclose(drain_timeout_s=0)
        return
    if name != "isolation":
        raise ValueError(f"unknown witness: {name}")
    roster = Roster(LimitsConfig(send_queue_max=1, send_timeout_s=3600))
    slow, fast = Socket(), Socket()
    fast.automatic = True
    try:
        await roster.add(slow, "student", "en")
        await roster.add(fast, "student", "en")
        for seq in range(1, 5):
            roster.broadcast({"type": "caption", "seq": seq})
            await pump()
            require("isolation", trace + [f"broadcast={seq}"], list(range(1, seq + 1)), fast.sent)
            require("isolation", trace, [], fast.closed)
        require("retry_close", trace, [1013], slow.closed)
        require("isolation", trace, [], slow.sent)
    finally:
        slow.automatic = True
        if slow.gate is not None and not slow.gate.done():
            slow.release()
        await roster.aclose()


async def check(depth, case=None, only_witness=None):
    counts = dict(attempted=0, legal=0, violations=0)
    violations = {}
    witnesses = {}

    def record(error):
        counts["violations"] += 1
        violations.setdefault(error.detail["property"], error.detail)

    if only_witness is None:
        for capacity in (1, 2):
            sequences = [tuple(case.split(","))] if case else (
                seq for size in range(depth + 1) for seq in itertools.product(OPERATIONS, repeat=size)
            )
            for sequence in sequences:
                if any(op not in OPERATIONS for op in sequence):
                    raise ValueError("unknown operation")
                counts["attempted"] += 1
                try:
                    counts["legal"] += bool(await scenario(sequence, capacity))
                except Violation as error:
                    record(error)
    if case is None:
        for name in (only_witness,) if only_witness else ("real_timeout", "isolation"):
            try:
                await witness(name)
                witnesses[name] = "PASS"
            except Violation as error:
                witnesses[name] = "FAIL"
                record(error)
    return dict(status="FAIL" if violations else "PASS", sync_commit=SYNC_COMMIT,
                depth=depth if not case and not only_witness else None, case=case,
                counts=counts, witnesses=witnesses, violations=list(violations.values()))


def mutation_check():
    mutations = (
        ("unbounded_queue", "capacity", ("--case", "send,send,send"),
         "delivery.py", "asyncio.Queue(maxsize=maxsize)", "asyncio.Queue(maxsize=0)"),
        ("fill_reserved_slot", "history_reservation", ("--case", "send_history,send"),
         "delivery.py", "self._queue.qsize() >= self._queue.maxsize - 1",
         "self._queue.qsize() >= self._queue.maxsize"),
        ("disconnect_on_history_drop", "history_reservation", ("--case", "send_history,send"),
         "delivery.py", "self._queue.maxsize - 1:\n            return False",
         "self._queue.maxsize - 1:\n            self._kill()\n            return False"),
        ("reverse_queue", "fifo", ("--case", "send,send,dispatch"),
         "delivery.py", "asyncio.Queue(maxsize=maxsize)", "asyncio.LifoQueue(maxsize=maxsize)"),
        ("accept_after_close", "admission", ("--case", "close,send"),
         "delivery.py", "if not self._alive:\n            return False", "if False:\n            return False"),
        ("forget_retry", "retry_close", ("--case", "send,send"),
         "delivery.py", "await ws.close(code=self._close_code)", "await asyncio.sleep(0)"),
        ("lose_drain", "drain", ("--case", "send,close"),
         "delivery.py", "await asyncio.wait_for(self._queue.join(), timeout=drain_timeout_s)",
         "pass"),
        ("ignore_timeout", "timeout", ("--witness", "real_timeout"),
         "delivery.py", "            self._kill()\n        except Exception:",
         "            return\n        except Exception:"),
        ("ignore_transport_error", "admission", ("--case", "send,dispatch,error"),
         "delivery.py", "            self._kill()\n\n    def _kill", "            pass\n\n    def _kill"),
        ("shared_sender", "isolation", ("--witness", "isolation"),
         "roster.py", "self._senders[client.id] = sender",
         "self._senders[client.id] = next(iter(self._senders.values()), sender)"),
    )
    results = []
    for name, property_name, arguments, filename, old, new in mutations:
        healthy = asyncio.run(check(0, arguments[1] if arguments[0] == "--case" else None,
                                    arguments[1] if arguments[0] == "--witness" else None))
        if (healthy["status"] != "PASS"
                or (arguments[0] == "--case" and healthy["counts"]["legal"] == 0)):
            raise RuntimeError(f"mutation witness is not healthy: {name}")
        with tempfile.TemporaryDirectory(prefix="client-delivery-formal-") as directory:
            scratch = Path(directory)
            shutil.copytree(ROOT / "server", scratch / "server",
                            ignore=shutil.ignore_patterns("__pycache__"))
            script = scratch / "scripts/formal/client_delivery.py"
            script.parent.mkdir(parents=True)
            shutil.copy2(__file__, script)
            target = scratch / "server" / filename
            source = target.read_text(encoding="utf-8")
            if source.count(old) != 1:
                raise RuntimeError(f"mutation site changed: {name}")
            target.write_text(source.replace(old, new), encoding="utf-8")
            child = subprocess.run([sys.executable, "-B", str(script), *arguments], cwd=scratch,
                                   capture_output=True, text=True, encoding="utf-8", timeout=30)
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
    parser.add_argument("--depth", type=int, default=4, choices=range(0, 7))
    parser.add_argument("--case", help="one comma-separated schedule")
    parser.add_argument("--witness", choices=("real_timeout", "isolation"))
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if args.case and args.witness:
        parser.error("--case and --witness are mutually exclusive")
    sys.path.insert(0, str(ROOT))
    logging.disable(logging.CRITICAL)
    result = asyncio.run(check(args.depth, args.case, args.witness))
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
