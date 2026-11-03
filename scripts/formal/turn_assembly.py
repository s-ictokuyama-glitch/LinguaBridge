"""Exhaust bounded operation sequences against the real TurnAssembler."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import itertools
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SYNC_COMMIT = "d78968533639ca29108318dd9e572407f26c1c10"
PARTS = {
    "normal": ("教科書", None, .5),
    "weak": ("この部分がですね。", None, .5),
    "reject": ("原子の", None, .5),
    "predicate": ("同じです", "predicate_end", .5),
    "strong": ("終わり！", "strong_end", .5),
    "long": ("教科書", None, 1.5),
}
OPERATIONS = (*PARTS, "gap", "reserve", "preview", "boundary", "flush", "reset", "discard")
PROFILES = {
    "simple": ("simple", 2, 1.0),
    "count": ("morph", 2, 100.0),
    "span": ("morph", 12, 1.0),
    "default": ("morph", 12, 30.0),
}


class Violation(Exception):
    def __init__(self, property_name, trace, expected, actual):
        self.detail = dict(property=property_name, trace=list(trace),
                           expected=expected, actual=actual)


def require(name, trace, expected, actual):
    if actual != expected:
        raise Violation(name, trace, expected, actual)


@dataclass
class PartRecord:
    text: str
    start: float
    end: float
    asr_ms: int
    closed_at: float
    audio_s: float


@dataclass
class Ledger:
    pending: list[PartRecord] = field(default_factory=list)
    current_id: int | None = None
    next_id: int = 1
    revision: int = 0
    clock: float = 0.0
    serial: int = 0

    def reserve(self):
        if self.current_id is None:
            self.current_id = self.next_id
            self.next_id += 1
            self.revision = 0

    def release(self):
        self.current_id = None
        self.revision = 0


def scenario(operations, profile):
    from server.turn.assembler import TurnAssembler, TurnPart
    from server.turn.boundary import SurfaceBoundaryClassifier

    strategy, max_segments, max_span = PROFILES[profile]
    assembler = TurnAssembler(SurfaceBoundaryClassifier(), strategy=strategy,
                              max_segments=max_segments, max_turn_s=max_span)
    ledger = Ledger()
    trace = []

    def snapshot():
        require("pending_state", trace, bool(ledger.pending), assembler.has_pending)
        require("turn_identity", trace, ledger.current_id, assembler.current_turn_id)

    def output(turn, reason):
        require("termination", trace, reason is not None, turn is not None)
        if reason is None:
            return
        require("termination", trace, reason, turn.reason)
        require("conservation", trace,
                ["".join(p.text for p in ledger.pending), len(ledger.pending),
                 len(ledger.pending) > 1], [turn.text, turn.parts, turn.merged])
        require("metadata", trace,
                [ledger.pending[0].start, ledger.pending[-1].end,
                 sum(p.asr_ms for p in ledger.pending), ledger.pending[-1].closed_at,
                 sum(p.audio_s for p in ledger.pending)],
                [turn.t_start, turn.t_end, turn.asr_ms, turn.closed_at, turn.audio_s])
        require("turn_identity", trace, ledger.current_id, turn.turn_id)
        ledger.pending.clear()
        ledger.release()

    def step(operation):
        trace.append(operation)
        if operation in PARTS:
            suffix, ending, duration = PARTS[operation]
            ledger.serial += 1
            record = PartRecord(f"【{ledger.serial}】{suffix}", ledger.clock,
                                ledger.clock + duration, ledger.serial * 7,
                                100 + ledger.serial * .25, duration)
            ledger.clock = record.end
            ledger.reserve()
            ledger.pending.append(record)
            reason = None
            if strategy == "simple":
                reason = "segment"
            elif ending:
                reason = ending
            elif (len(ledger.pending) >= max_segments
                  or record.end - ledger.pending[0].start >= max_span):
                reason = "max_turn"
            output(assembler.add_segment(TurnPart(
                record.text, record.start, record.end, record.asr_ms,
                record.closed_at, record.audio_s)), reason)
        elif operation == "gap":
            ledger.clock += .5
        elif operation == "reserve":
            ledger.reserve()
            require("turn_identity", trace, ledger.current_id, assembler.reserve_turn_id())
        elif operation == "preview":
            ledger.reserve()
            ledger.revision += 1
            text = f"途中{ledger.serial}"
            turn_id, revision, actual = assembler.preview(text)
            require("turn_identity", trace, ledger.current_id, turn_id)
            require("preview_revision", trace, ledger.revision, revision)
            require("preview_text", trace, "".join(p.text for p in ledger.pending) + text, actual)
        elif operation in ("boundary", "flush"):
            turn = assembler.on_boundary() if operation == "boundary" else assembler.flush()
            output(turn, operation if ledger.pending else None)
            ledger.release()
        elif operation == "reset":
            ledger.pending.clear()
            ledger.release()
            assembler.reset()
        elif operation == "discard":
            if not ledger.pending:
                ledger.release()
            assembler.discard_reserved()
        else:
            raise ValueError(f"unknown operation: {operation}")
        snapshot()

    snapshot()
    for operation in operations:
        step(operation)
    step("flush")
    step("flush")


def check(depth, case=None, profile=None):
    violations = {}
    counts = dict(sequences=0, scenarios=0, failed=0)
    sequences = [tuple(case.split(","))] if case else (
        seq for size in range(depth + 1) for seq in itertools.product(OPERATIONS, repeat=size)
    )
    for sequence in sequences:
        if any(operation not in OPERATIONS for operation in sequence):
            raise ValueError("unknown operation")
        counts["sequences"] += 1
        for name in (profile,) if profile else PROFILES:
            counts["scenarios"] += 1
            try:
                scenario(sequence, name)
            except Violation as error:
                counts["failed"] += 1
                key = error.detail["property"]
                previous = violations.get(key)
                if previous is None or len(error.detail["trace"]) < len(previous["trace"]):
                    violations[key] = dict(profile=name, **error.detail)
    return dict(status="FAIL" if violations else "PASS", sync_commit=SYNC_COMMIT,
                depth=depth if not case else None, case=case,
                profiles=[profile] if profile else list(PROFILES),
                counts=counts, violations=list(violations.values()))


def mutation_check():
    mutations = (
        ("drop_part", "conservation", "default", "normal,normal,flush",
         "self._pending.add(part)",
         "if self._pending.empty:\n            self._pending.add(part)"),
        ("reverse_parts", "conservation", "default", "normal,weak,flush",
         'text="".join(p.text for p in self.parts),',
         'text="".join(p.text for p in reversed(self.parts)),'),
        ("forget_asr_sum", "metadata", "default", "normal,normal,flush",
         "asr_ms=sum(p.asr_ms for p in self.parts),", "asr_ms=self.parts[-1].asr_ms,"),
        ("count_gap_as_audio", "metadata", "default", "normal,gap,normal,flush",
         "audio_s=sum(p.audio_s for p in self.parts),",
         "audio_s=self.parts[-1].t_end - self.parts[0].t_start,"),
        ("ignore_count", "termination", "count", "normal,normal",
         "len(self._pending.parts) >= self._max_segments", "False"),
        ("ignore_span", "termination", "span", "normal,normal",
         "self._pending.span_s() >= self._max_turn_s", "False"),
        ("keep_closed_id", "turn_identity", "default", "strong,preview",
         "self._pending = _Pending()\n        self._turn_id = None\n        self._revision = 0\n        return turn",
         "self._pending = _Pending()\n        self._revision = 0\n        return turn"),
        ("omit_preview_prefix", "preview_text", "default", "normal,preview",
         '"".join(p.text for p in self._pending.parts) + interim_text', "interim_text"),
        ("freeze_revision", "preview_revision", "default", "preview,preview",
         "self._revision += 1", "self._revision += 0"),
        ("discard_pending_id", "turn_identity", "default", "normal,discard",
         "if self._pending.empty:\n            self._turn_id = None",
         "if True:\n            self._turn_id = None"),
        ("reuse_reset_id", "turn_identity", "default", "reserve,reset,reserve",
         "def reset(self) -> None:\n        self._pending = _Pending()",
         "def reset(self) -> None:\n        self._turn_ids = itertools.count(1)\n        self._pending = _Pending()"),
        ("hide_pending", "pending_state", "default", "normal",
         "return not self._pending.empty", "return False"),
    )
    results = []
    for name, property_name, profile, case, old, new in mutations:
        healthy = check(0, case, profile)
        if healthy["status"] != "PASS" or healthy["counts"]["scenarios"] != 1:
            raise RuntimeError(f"mutation witness is not healthy: {name}")
        with tempfile.TemporaryDirectory(prefix="turn-assembly-formal-") as directory:
            scratch = Path(directory)
            shutil.copytree(ROOT / "server/turn", scratch / "server/turn",
                            ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy2(ROOT / "server/__init__.py", scratch / "server/__init__.py")
            script = scratch / "scripts/formal/turn_assembly.py"
            script.parent.mkdir(parents=True)
            shutil.copy2(__file__, script)
            target = scratch / "server/turn/assembler.py"
            source = target.read_text(encoding="utf-8")
            if source.count(old) != 1:
                raise RuntimeError(f"mutation site changed: {name}")
            target.write_text(source.replace(old, new), encoding="utf-8")
            child = subprocess.run([sys.executable, "-X", "utf8", "-B", str(script), "--case", case,
                                    "--profile", profile], cwd=scratch,
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
    parser.add_argument("--case", help="one comma-separated operation sequence")
    parser.add_argument("--profile", choices=PROFILES)
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    result = check(args.depth, args.case, args.profile)
    if args.mutation_check:
        if result["status"] == "PASS":
            result["mutations"] = mutation_check()
        else:
            result["mutation_check_skipped"] = "current code has counterexamples"
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output, encoding="utf-8")
    print(output, end="")
    return int(result["status"] == "FAIL")


if __name__ == "__main__":
    raise SystemExit(main())
