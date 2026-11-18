"""Check which speech reaches ASR and both subtitle streams across a lesson boundary."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, dataclass
from itertools import groupby, product
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).parent)]
from lesson_liveness import Socket, Translation
from silero_contract import model_scope
from server.asr.base import ASRResult
from server.config import load_config
from server.connection import Connection
from server.lesson import Lesson
from server.pipeline import Pipeline
from server.roster import Roster

PACKET = 1600
FRAME = 512
SILENCE = bytes(PACKET * 2)
WORDS = {1000: "停止前", 2000: "再開直後", 3000: "次の説明"}


@dataclass(frozen=True)
class Case:
    action: str
    packets: int
    before: int
    after: int


class FrameScores:
    """Assign a high Silero score to a marked frame, independent of preceding audio."""

    def run(self, outputs, inputs):
        speech = np.any(inputs["input"][0, -FRAME:])
        return np.array([[float(speech)]], dtype=np.float32), inputs["h"], inputs["c"]


def text_for(runs):
    return "と".join(WORDS[marker] for marker, count in runs) + "です。"


class Recognition:
    def __init__(self):
        self.calls = []

    def warmup(self):
        pass

    def transcribe(self, pcm, sample_rate):
        if sample_rate != 16000:
            raise ValueError(f"unexpected ASR rate: {sample_rate}")
        runs = [[int(marker), sum(1 for _ in samples)]
                for marker, samples in groupby(pcm[pcm != 0])]
        self.calls.append(runs)
        return ASRResult(text_for(runs))


async def classroom(config, case):
    asr = Recognition()
    roster = Roster(config.limits)
    with model_scope(FrameScores()):
        pipeline = Pipeline(roster, config, asr, Translation())
    lesson = Lesson(pipeline, config, join_code="1234", teacher_code="123456")
    teacher = Socket()
    wire = Connection(teacher, "127.0.0.1", lesson, config.limits)
    students = {lang: Socket() for lang in ("en", "zh")}
    differences = []
    observed = {}
    cursors = Counter()

    async def control(action):
        await wire.receive(json.dumps({"type": "control", "action": action}))

    async def send(pcm):
        for offset in range(0, len(pcm), PACKET):
            await wire.receive(pcm[offset:offset + PACKET].tobytes())

    def compare(phase, field, expected, actual):
        if actual != expected:
            differences.append(dict(phase=phase, field=field, expected=expected, actual=actual))

    async def checkpoint(phase, marker, voiced_frames):
        await pipeline.drain(timeout=5)
        await asyncio.wait_for(asyncio.gather(*(s._queue.join() for s in roster._senders.values())), 5)
        if any(s.closed is not None for s in (teacher, *students.values())):
            raise RuntimeError("a fixture connection closed")
        if not all(not task.done() for task in [*pipeline._tasks, pipeline.captions._worker_task]):
            raise RuntimeError("a classroom worker stopped")
        calls = asr.calls[cursors["asr"]:]
        cursors["asr"] = len(asr.calls)
        expected_calls = [[[marker, voiced_frames * FRAME]]] if voiced_frames >= 3 else []
        words = [text_for(runs) for runs in expected_calls]
        finals = [m for m in teacher.frames[cursors["teacher"]:] if m["type"] == "asr_final"]
        cursors["teacher"] = len(teacher.frames)
        row = dict(asr=calls, teacher=[m["ja"] for m in finals])
        compare(phase, "asr", expected_calls, calls)
        compare(phase, "teacher", words, row["teacher"])
        for lang, socket in students.items():
            captions = [m for m in socket.frames[cursors[lang]:] if m["type"] == "caption"]
            cursors[lang] = len(socket.frames)
            row[lang] = [[m["ja"], m["lang"], m["text"]] for m in captions]
            compare(phase, lang, [[word, lang, f"{lang}:{word}"] for word in words], row[lang])
            compare(phase, f"{lang}_seq", [m["seq"] for m in finals], [m["seq"] for m in captions])
        observed[phase] = row

    try:
        await wire.receive(json.dumps(dict(type="join", role="teacher", code="123456")))
        for lang, socket in students.items():
            await roster.add(socket, "student", lang)
        await pipeline.start()
        await asyncio.wait_for(pipeline._warmup_task, 5)
        if not pipeline.ready:
            raise RuntimeError("warmup failed")
        await control("start")
        compare("before", "start_state", "live", lesson.state)
        old = np.zeros(case.packets * PACKET, dtype="<i2")
        whole = len(old) // FRAME * FRAME
        old[whole - case.before * FRAME:whole] = 1000
        await send(old)
        await control(case.action)
        compare("boundary", "state", {"pause": "paused", "end": "ended"}[case.action], lesson.state)
        await checkpoint("before", 1000, case.before)
        await control("start")
        compare("after", "start_state", "live", lesson.state)
        for phase, marker, count in (("after", 2000, case.after), ("continuation", 3000, 3)):
            packet = np.zeros(PACKET, dtype="<i2")
            packet[:count * FRAME] = marker
            await send(packet)
            # Each block is 1.6 s (50 complete Silero frames), ending in long silence.
            for _ in range(15):
                await wire.receive(SILENCE)
            await checkpoint(phase, marker, count)
        seqs = [m["seq"] for m in teacher.frames if m["type"] == "asr_final"]
        compare("lesson", "sequence", list(range(1, len(seqs) + 1)), seqs)
    finally:
        await pipeline.stop()
    return differences, observed


async def search(cases):
    config = load_config(ROOT / "config.yaml")
    if (config.vad.engine, config.turn.strategy, config.turn.morph.start_speech_ms,
            config.partial.enabled, config.recording.default_on) != ("silero", "morph", 96, False, False):
        raise ValueError("review the input ledger against config.yaml")
    passed = continuation_ok = 0
    failures = []
    first = {}
    for case in cases:
        differences, observed = await asyncio.wait_for(classroom(config, case), 15)
        if len(cases) == 1:
            return dict(status="FAIL" if differences else "PASS", case=asdict(case),
                        differences=differences, observed=observed)
        continuation_ok += not any(d["phase"] == "continuation" for d in differences)
        if differences:
            failures.append(f"{case.action} {case.packets} {case.before} {case.after}")
            first.setdefault(case.action, dict(case=asdict(case), differences=differences, observed=observed))
        else:
            passed += 1
    return dict(status="FAIL" if failures else "PASS", cases=len(cases), passed=passed, failed=len(failures),
                continuation_ok=continuation_ok, failed_cases=failures, first_counterexamples=first)


MUTATIONS = (
    ("drop_flushed_audio", "pipeline.py", "segment = self._segmenter.flush()",
     "self._segmenter.flush()\n        segment = None", Case("end", 1, 3, 3), "before", "asr"),
    ("reuse_opening_audio", "audio/vad.py", "segment = self._close(forced=False) if self._frames else None",
     "if not self._frames:\n            self._pending = np.zeros(0, dtype=np.int16)\n"
     "            self._continue_after_force = False\n            return None\n"
     "        segment = self._close(forced=False)", Case("end", 1, 2, 1), "after", "asr"),
    ("skip_final_chinese", "caption_delivery.py", "if self.roster.send(client, captions[selected]):",
     'if (selected != "zh" or job.utterance.seq < 3) and self.roster.send(client, captions[selected]):',
     Case("end", 1, 3, 3), "continuation", "zh"),
    ("skip_sequence", "caption_history.py", "self._seq += 1", "self._seq += 2",
     Case("end", 1, 3, 3), "lesson", "sequence"),
    ("pause_instead_of_end", "lesson.py", '"end": "ended"', '"end": "paused"',
     Case("end", 1, 3, 3), "boundary", "state"),
)


def mutations():
    with tempfile.TemporaryDirectory(prefix="lesson-audio-boundary-") as directory:
        scratch = Path(directory)
        shutil.copytree(ROOT / "server", scratch / "server", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "config.yaml", scratch)
        scripts = scratch / "scripts/formal"
        scripts.mkdir(parents=True)
        for name in (Path(__file__).name, "lesson_liveness.py", "silero_contract.py"):
            shutil.copy2(ROOT / "scripts/formal" / name, scripts)

        def probe(case):
            child = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", str(scripts / Path(__file__).name),
                                    "--case", case.action, str(case.packets), str(case.before), str(case.after)],
                                   cwd=scratch, capture_output=True,
                                   encoding="utf-8", timeout=30)
            if child.returncode not in (0, 1):
                raise RuntimeError(f"mutation probe exited {child.returncode}: {child.stdout} {child.stderr}")
            report = json.loads(child.stdout)
            if child.returncode != {"PASS": 0, "FAIL": 1}.get(report["status"]):
                raise RuntimeError(f"mutation probe incomplete: {report}; {child.stderr}")
            return report

        controls = {}
        for _, _, _, _, case, _, _ in MUTATIONS:
            if case not in controls:
                control = probe(case)
                if control["status"] != "PASS":
                    return dict(status="INCONCLUSIVE", control=control)
                controls[case] = control
        results = {}
        for name, file, before, after, case, phase, field in MUTATIONS:
            target = scratch / "server" / file
            source = target.read_bytes()
            code = source.decode("utf-8").replace("\r\n", "\n")
            if code.count(before) != 1:
                raise ValueError(f"review mutation {name} in {file}")
            target.write_text(code.replace(before, after), encoding="utf-8")
            try:
                broken = probe(case)
                witness = next((d for d in broken["differences"]
                                if (d["phase"], d["field"]) == (phase, field)), None)
                if witness is None:
                    raise RuntimeError(f"missed mutation {name}: {broken}")
                results[name] = witness
            finally:
                target.write_bytes(source)
        for case, control in controls.items():
            if probe(case) != control:
                raise RuntimeError("restored control differs")
        return dict(status="PASS", results=results, restored_control=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--case", nargs=4, metavar=("ACTION", "PACKETS", "BEFORE", "AFTER"))
    group.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    cases = [Case(*row) for row in product(("pause", "end"), range(1, 9), range(4), range(4))]
    if args.case:
        try:
            case = Case(args.case[0], *map(int, args.case[1:]))
            if case not in cases:
                raise ValueError
        except ValueError:
            parser.error("choose pause/end, 1..8 packets, 0..3 before frames and 0..3 after frames")
        cases = [case]
    report = {}
    try:
        report = asyncio.run(search(cases))
        if args.mutation_check:
            checks = mutations()
            restored = asyncio.run(search(cases))
            if restored != report:
                raise RuntimeError("restored full search differs")
            report.update(mutations=checks, restored_matches=True)
            if checks["status"] == "INCONCLUSIVE" and report["status"] == "PASS":
                raise RuntimeError("mutation control disagrees with full search")
    except Exception as error:
        if report:
            report["search_status"] = report["status"]
        report.update(status="ERROR", error=f"{type(error).__name__}: {error}")
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return {"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
