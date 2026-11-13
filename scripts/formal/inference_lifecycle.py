"""Enumerate bounded inference lifecycles against the actual InferenceRunner."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from concurrent.futures import Executor, Future
from dataclasses import dataclass
import itertools
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from server.inference import InferenceRunner

SYNC_COMMIT = "4efcb9f6edd2b1e39aea9c173d9b8b29870861f1"
OPERATIONS = ("ok", "error", "timeout_error", "expire", "finish", "cancel0", "cancel1", "stop",
              "finish_cancel", "cancel_finish")
MAX_CALLS = 2


class Violation(Exception):
    def __init__(self, name, trace, expected, actual):
        self.detail = dict(property=name, trace=list(trace), expected=expected, actual=actual)


def require(name, trace, expected, actual):
    if expected != actual:
        raise Violation(name, trace, expected, actual)


class ControlledExecutor(Executor):
    def __init__(self):
        self.active = None
        self.queued = deque()
        self.submitted = asyncio.Queue()
        self.started = []
        self.finished = []
        self.stopped = False

    def submit(self, operation):
        if self.stopped:
            raise RuntimeError("executor stopped")
        future = Future()
        self.queued.append((future, operation))
        self._start_next()
        self.submitted.put_nowait(future)
        return future

    def _start_next(self):
        while self.active is None and self.queued:
            future, operation = self.queued.popleft()
            if future.set_running_or_notify_cancel():
                self.active = (future, operation)
                self.started.append(operation.job_id)

    def complete(self):
        future, operation = self.active
        self.finished.append(operation.job_id)
        try:
            future.set_result(operation())
        except Exception as error:
            future.set_exception(error)
        self.active = None
        self._start_next()

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.stopped = True
        if cancel_futures:
            while self.queued:
                future, _ = self.queued.popleft()
                future.cancel()


@dataclass
class Call:
    kind: str
    worker: str
    caller: str
    task: asyncio.Task
    future: Future | None


def operation_for(job_id, kind):
    def operation():
        if kind == "error":
            raise ValueError(f"job{job_id}")
        if kind == "timeout_error":
            raise TimeoutError(f"job{job_id}")
        return f"job{job_id}"
    operation.job_id = job_id
    return operation


def outcome(task):
    if not task.done():
        return "pending"
    if task.cancelled():
        return "cancelled"
    error = task.exception()
    if error is not None:
        return f"raised:{type(error).__name__}"
    result = task.result()
    name = type(result).__name__
    if name == "InferenceSucceeded":
        return f"success:{result.value}"
    if name == "InferenceFailed":
        return f"failure:{type(result.error).__name__}"
    return name


async def callbacks():
    # asyncio Future chaining can schedule another callback on the next loop turn.
    for _ in range(8):
        await asyncio.sleep(0)


async def scenario(sequence):
    executor = ControlledExecutor()
    runner = InferenceRunner(executor)
    calls = []
    started = []
    finished = []
    stopped = False
    trace = []

    async def snapshot():
        await callbacks()
        require("execution_order", trace, started, executor.started)
        require("execution_order", trace, finished, executor.finished)
        for call in calls:
            if call.future is not None:
                require("queued_cancellation", trace, call.worker == "cancelled", call.future.cancelled())
            require("caller_outcome", trace, call.caller, outcome(call.task))

    async def step(event):
        nonlocal stopped
        trace.append(event)
        if event in ("ok", "error", "timeout_error", "expire"):
            if len(calls) == MAX_CALLS:
                return False
            job_id = len(calls)
            task = asyncio.create_task(runner.run(operation_for(job_id, event),
                                                timeout=0 if event == "expire" else None))
            if stopped:
                await callbacks()
                calls.append(Call(event, "rejected", "failure:RuntimeError", task, None))
            else:
                future = await executor.submitted.get()
                worker = "queued" if any(c.worker == "running" for c in calls) else "running"
                if worker == "running":
                    started.append(job_id)
                caller = "pending"
                if event == "expire":
                    caller = "InferenceTimedOut"
                    if worker == "queued":
                        worker = "cancelled"
                calls.append(Call(event, worker, caller, task, future))
        elif event in ("finish", "finish_cancel", "cancel_finish"):
            active = next((i for i, c in enumerate(calls) if c.worker == "running"), None)
            if active is None:
                return False
            call = calls[active]
            if event != "finish" and call.caller != "pending":
                return False
            if event == "cancel_finish":
                call.task.cancel()
            call.worker = "done"
            finished.append(active)
            if event != "finish":
                call.caller = "cancelled"
            elif call.caller == "pending":
                call.caller = {"error": "failure:ValueError", "timeout_error": "failure:TimeoutError"}.get(
                    call.kind, f"success:job{active}")
            executor.complete()
            if event == "finish_cancel":
                call.task.cancel()
            waiting = next((i for i, c in enumerate(calls) if c.worker == "queued"), None)
            if waiting is not None:
                calls[waiting].worker = "running"
                started.append(waiting)
        elif event.startswith("cancel"):
            index = int(event[-1])
            if index >= len(calls) or calls[index].caller != "pending":
                return False
            call = calls[index]
            call.task.cancel()
            call.caller = "cancelled"
            if call.worker == "queued":
                call.worker = "cancelled"
        else:
            if stopped:
                return False
            stopped = True
            runner.stop()
            for call in calls:
                if call.worker == "queued":
                    call.worker = "cancelled"
                    call.caller = "cancelled"
        await snapshot()
        return True

    try:
        for event in sequence:
            if not await step(event):
                return False
        while any(c.worker == "running" for c in calls):
            await step("finish")
        if not stopped:
            await step("stop")
        return True
    finally:
        for call in calls:
            if not call.task.done():
                call.task.cancel()
        await asyncio.gather(*(c.task for c in calls), return_exceptions=True)


async def native_witness(first):
    runner = InferenceRunner(name="formal-inference")
    entered = [threading.Event() for _ in range(3)]
    release = [threading.Event() for _ in range(3)]
    started = []
    tasks = []
    stop_thread = None
    trace = [f"native_{first}"]

    def operation(index):
        started.append(index)
        entered[index].set()
        if not release[index].wait(5):
            raise RuntimeError("native gate was not released")
        return f"job{index}"

    try:
        tasks.append(asyncio.create_task(runner.run(lambda: operation(0), timeout=0.01 if first == "expire" else None)))
        await asyncio.sleep(0)
        if not entered[0].wait(2):
            raise RuntimeError("native worker did not start")
        if first == "expire":
            await tasks[0]
        if first == "cancel":
            tasks[0].cancel()
            await callbacks()
        for index in (1, 2):
            tasks.append(asyncio.create_task(runner.run(lambda i=index: operation(i), timeout=None)))
            await callbacks()
        entered[1].wait(0.1)
        require("serial_execution", trace, [0], list(started))
        if first == "stop":
            stop_started = threading.Event()
            stop_returned = threading.Event()

            def stop():
                stop_started.set()
                runner.stop()
                stop_returned.set()

            stop_thread = threading.Thread(target=stop)
            stop_thread.start()
            if not stop_started.wait(2):
                raise RuntimeError("native stop thread did not start")
            require("nonblocking_stop", trace, True, stop_returned.wait(2))
            await callbacks()
            require("queued_cancellation", trace, ["cancelled", "cancelled"], [outcome(t) for t in tasks[1:]])
        for index in range(3):
            if first == "stop" and index > 0:
                break
            if not entered[index].wait(2):
                raise RuntimeError(f"native job {index} did not start")
            release[index].set()
            if index + 1 < 3 and first != "stop":
                if not entered[index + 1].wait(2):
                    raise RuntimeError("native next job did not start")
        await asyncio.gather(*tasks, return_exceptions=True)
        expected = ["success:job0", "success:job1", "success:job2"]
        if first in ("expire", "cancel"):
            expected[0] = "InferenceTimedOut" if first == "expire" else "cancelled"
        if first == "stop":
            expected[1:] = ["cancelled", "cancelled"]
        require("caller_outcome", trace, expected, [outcome(t) for t in tasks])
        return True
    finally:
        for gate in release:
            gate.set()
        if stop_thread is not None:
            stop_thread.join(timeout=2)
            if stop_thread.is_alive():
                raise RuntimeError("native stop thread did not exit")
        runner.stop()
        await asyncio.gather(*tasks, return_exceptions=True)


async def check(depth, case=None):
    attempted = legal = 0
    violations = {}
    sequences = [tuple(case.split(","))] if case else (
        seq for size in range(depth + 1) for seq in itertools.product(OPERATIONS, repeat=size))
    for sequence in sequences:
        if any(event not in OPERATIONS for event in sequence):
            raise ValueError("unknown operation")
        attempted += 1
        try:
            async with asyncio.timeout(3):
                legal += bool(await scenario(sequence))
        except Violation as error:
            violations.setdefault(error.detail["property"], error.detail)
    if case is not None and legal == 0 and not violations:
        raise ValueError(f"case is not a legal lifecycle: {case}")
    native = 0
    if case is None:
        for first in ("ok", "expire", "cancel", "stop"):
            try:
                await native_witness(first)
                native += 1
            except Violation as error:
                violations.setdefault(error.detail["property"], error.detail)
    return dict(status="FAIL" if violations else "PASS", sync_commit=SYNC_COMMIT,
                depth=depth if case is None else None, max_calls=MAX_CALLS, attempted=attempted,
                legal=legal, native_witnesses=native, violations=list(violations.values()))


MUTATIONS = (
    ("parallel_workers", "serial_execution", None, "ThreadPoolExecutor(1,", "ThreadPoolExecutor(2,"),
    ("keep_queued_on_stop", "queued_cancellation", "ok,ok,stop",
     "cancel_futures=True", "cancel_futures=False"),
    ("swallow_cancel", "caller_outcome", "ok,cancel0",
     "except asyncio.CancelledError:\n            future.cancel()\n            raise",
     'except asyncio.CancelledError:\n            future.cancel()\n            return InferenceFailed(RuntimeError("cancel swallowed"))'),
    ("operation_timeout_as_deadline", "caller_outcome", "timeout_error,finish",
     "if timeout is None:", "if False:"),
    ("duplicate_execution", "execution_order", "ok,finish",
     "future = self._executor.submit(operation)",
     "future = self._executor.submit(operation)\n            self._executor.submit(operation)"),
    ("blocking_stop", "nonblocking_stop", None, "shutdown(wait=False", "shutdown(wait=True"),
)


def mutation_check():
    results = []
    for name, property_name, case, before, after in MUTATIONS:
        healthy = asyncio.run(check(0, case))
        if healthy["status"] != "PASS" or healthy["legal"] != 1:
            raise RuntimeError(f"mutation witness is not healthy: {name}")
        with tempfile.TemporaryDirectory(prefix="inference-lifecycle-") as directory:
            scratch = Path(directory)
            (scratch / "server").mkdir()
            (scratch / "server/__init__.py").write_text("", encoding="utf-8")
            source = (ROOT / "server/inference.py").read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"mutation site changed: {name}")
            target = scratch / "server/inference.py"
            target.write_text(source.replace(before, after), encoding="utf-8")
            script = scratch / "scripts/formal/inference_lifecycle.py"
            script.parent.mkdir(parents=True)
            shutil.copy2(__file__, script)
            command = [sys.executable, "-X", "utf8", "-B", str(script), "--depth", "0"]
            if case:
                command.extend(["--case", case])
            child = subprocess.run(command, cwd=scratch, capture_output=True, text=True,
                                   encoding="utf-8", timeout=30)
            if child.returncode != 1:
                raise RuntimeError(f"mutation did not report a violation: {name}\n{child.stderr}")
            report = json.loads(child.stdout)
            witness = next((v for v in report["violations"] if v["property"] == property_name), None)
            if witness is None:
                raise RuntimeError(f"mutation missed {property_name}: {name}\n{child.stdout}")
            results.append(dict(name=name, healthy="PASS", detected=True, violation=witness))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depth", type=int, default=5)
    parser.add_argument("--case")
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if not 0 <= args.depth <= 6:
        parser.error("depth must be between 0 and 6")
    report = asyncio.run(check(args.depth, args.case))
    if args.mutation_check and report["status"] == "PASS":
        report["mutations"] = mutation_check()
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return report["status"] != "PASS"


if __name__ == "__main__":
    sys.exit(main())
