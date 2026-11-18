"""Explore capture and message delivery on either side of a teacher pause or lesson end."""
from __future__ import annotations

import argparse
import asyncio
from base64 import b64decode
from collections import Counter
from itertools import groupby, product
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zlib

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]
from lesson_audio_boundary import FrameScores
from lesson_liveness import Socket, Translation
from silero_contract import model_scope
from server.asr.base import ASRResult
from server.config import load_config
from server.connection import Connection
from server.lesson import Lesson
from server.pipeline import Pipeline
from server.roster import Roster


ACTIONS = ("pause", "end")
PERIODS = ("before", "stopped")
QUANTA = range(76)
DELIVERY = ("immediate", "queued")
PACKET_BYTES = 3200
WORDS = {1000: "停止前", 2000: "停止中", 3000: "再開直後", 4000: "次の説明"}


class Recognition:
    def __init__(self):
        self.calls = []

    def warmup(self):
        pass

    def transcribe(self, pcm, sample_rate):
        if sample_rate != 16000:
            raise ValueError(f"unexpected ASR rate {sample_rate}")
        markers = [int(marker) for marker, _ in groupby(pcm[pcm != 0])]
        self.calls.append(markers)
        return ASRResult("と".join(WORDS[m] for m in markers) + "です。")


def capture(cases):
    child = subprocess.run(
        ["node", str(Path(__file__).with_suffix(".cjs")), str(ROOT)],
        input=json.dumps(cases), capture_output=True, text=True, encoding="utf-8",
        timeout=120, check=True,
    )
    rows = child.stdout.splitlines()
    if len(rows) != len(cases):
        raise RuntimeError("missing capture histories")
    for row in rows:
        yield {name: zlib.decompress(b64decode(data, validate=True)) for name, data in json.loads(row).items()}


async def classroom(config, case, phases):
    asr = Recognition()
    roster = Roster(config.limits)
    with model_scope(FrameScores()):
        pipeline = Pipeline(roster, config, asr, Translation())
    lesson = Lesson(pipeline, config, join_code="1234", teacher_code="123456")
    teacher = Socket()
    wire = Connection(teacher, "127.0.0.1", lesson, config.limits)
    students = {lang: Socket() for lang in ("en", "zh")}
    differences = {}

    def compare(name, expected, actual):
        if expected != actual:
            differences[name] = dict(expected=expected, actual=actual)

    async def control(action, state):
        await wire.receive(json.dumps(dict(type="control", action=action)))
        if lesson.state != state:
            raise RuntimeError(f"{action} left the lesson in {lesson.state}")

    async def send(name):
        pcm = phases[name]
        for offset in range(0, len(pcm), PACKET_BYTES):
            await wire.receive(pcm[offset:offset + PACKET_BYTES])

    async def drain():
        await pipeline.drain(timeout=5)
        await asyncio.wait_for(asyncio.gather(*(s._queue.join() for s in roster._senders.values())), 5)

    try:
        await wire.receive(json.dumps(dict(type="join", role="teacher", code="123456")))
        for lang, socket in students.items():
            await roster.add(socket, "student", lang)
        await pipeline.start()
        await asyncio.wait_for(pipeline._warmup_task, 5)
        if not pipeline.ready:
            raise RuntimeError("fixture warmup failed")
        await control("start", "live")
        await send("before")
        await control(case[0], {"pause": "paused", "end": "ended"}[case[0]])
        await send("stopped")
        compare("stopped_packets", 0, len(phases["stopped"]) // PACKET_BYTES)
        await drain()
        await control("start", "live")
        seq = pipeline.captions.history.seq_head
        for phase, marker in (("first", 3000), ("following", 4000)):
            samples = np.frombuffer(phases[phase], dtype="<i2")
            compare(f"{phase}_new_samples", 6400, int(np.count_nonzero(samples == marker)))
            stale = {WORDS[m]: int(np.count_nonzero(samples == m)) for m in (1000, 2000) if np.any(samples == m)}
            compare(f"{phase}_old_pcm", {}, stale)
            call_at = len(asr.calls)
            teacher_at = len(teacher.frames)
            student_at = {lang: len(s.frames) for lang, s in students.items()}
            await send(phase)
            await drain()
            seq += 1
            word = WORDS[marker] + "です。"
            compare(f"{phase}_asr", [[marker]], asr.calls[call_at:])
            finals = [[f["seq"], f["ja"]] for f in teacher.frames[teacher_at:] if f["type"] == "asr_final"]
            compare(f"{phase}_teacher", [[seq, word]], finals)
            for lang, socket in students.items():
                captions = [[f["seq"], f["ja"], f["lang"], f["text"]]
                            for f in socket.frames[student_at[lang]:] if f["type"] == "caption"]
                compare(f"{phase}_{lang}", [[seq, word, lang, f"{lang}:{word}"]], captions)
    finally:
        await pipeline.stop()
    return differences


async def search(cases):
    config = load_config(ROOT / "config.yaml")
    if (config.vad.engine, config.turn.strategy, config.partial.enabled, config.recording.default_on) != (
            "silero", "morph", False, False):
        raise ValueError("review the input ledger against config.yaml")
    counts = Counter()
    examples = {}
    failed = following_ok = 0
    captures = capture(cases)
    for case, phases in zip(cases, captures, strict=True):
        differences = await asyncio.wait_for(classroom(config, case, phases), 15)
        if differences:
            failed += 1
        following_ok += not any(key.startswith("following_") for key in differences)
        counts.update(differences.keys())
        for key, difference in differences.items():
            examples.setdefault(key, dict(case=case, **difference))
    return dict(status="FAIL" if failed else "PASS", cases=len(cases), failed=failed,
                passed=len(cases) - failed, following_ok=following_ok,
                failures_by_property=dict(counts), first_counterexamples=examples)


def mutation_check():
    # This aligned stopped period is clean when delivery is immediate.
    case = ["pause", "stopped", "75", "immediate"]
    mutations = (
        ("send_while_stopped", "web/teacher.js", 'state.sessionState === "live" &&',
         "true &&", {"stopped_packets"}),
        ("resend_stopped_packet", "web/teacher.js", "    updateMicMeter(ev.data);",
         '    if (state.sessionState !== "live") state.previousPacket = ev.data;\n'
         '    else if (state.previousPacket) {\n'
         '      const voiced = new Int16Array(ev.data).some(sample => sample !== 0);\n'
         '      if (voiced && !state.wasVoiced) state.ws.send(state.previousPacket);\n'
         '      state.wasVoiced = voiced;\n'
         '    }\n'
         "    updateMicMeter(ev.data);",
         {"first_old_pcm", "following_old_pcm", "first_asr", "first_teacher", "first_en", "first_zh"}),
        ("drop_following_audio", "web/teacher.js", "      state.ws.send(ev.data);",
         "      if (!new Int16Array(ev.data).includes(4000)) state.ws.send(ev.data);",
         {"following_asr", "following_teacher", "following_en", "following_zh"}),
        ("mute_packet_onset", "web/audio-worklet.js", "this.port.postMessage(this.out.buffer.slice(0));",
         "this.out[0] = 0;\n        this.port.postMessage(this.out.buffer.slice(0));",
         {"first_new_samples", "following_new_samples"}),
        ("drop_following_chinese", "server/caption_delivery.py", "if self.roster.send(client, captions[selected]):",
         'if (selected != "zh" or job.utterance.seq == 1) and self.roster.send(client, captions[selected]):',
         {"following_zh"}),
    )
    with tempfile.TemporaryDirectory(prefix="teacher-audio-resume-") as directory:
        scratch = Path(directory)
        shutil.copytree(ROOT / "server", scratch / "server", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "config.yaml", scratch)
        (scratch / "web").mkdir()
        for filename in ("teacher.js", "audio-worklet.js"):
            shutil.copy2(ROOT / "web" / filename, scratch / "web")
        scripts = scratch / "scripts/formal"
        scripts.mkdir(parents=True)
        for filename in ("teacher_audio_resume.py", "teacher_audio_resume.cjs", "teacher_audio.cjs",
                         "lesson_audio_boundary.py", "lesson_liveness.py", "silero_contract.py"):
            shutil.copy2(Path(__file__).with_name(filename), scripts)

        def check():
            child = subprocess.run(
                [sys.executable, "-I", "-X", "utf8", "-B", str(scripts / "teacher_audio_resume.py"), "--case", *case],
                capture_output=True, encoding="utf-8", timeout=45, cwd=scratch,
            )
            if child.returncode not in (0, 1):
                raise RuntimeError(f"scratch run failed: {child.stderr} {child.stdout}")
            report = json.loads(child.stdout)
            if report["status"] != ("PASS" if child.returncode == 0 else "FAIL"):
                raise RuntimeError(f"inconsistent scratch exit code {child.returncode}: {report}")
            return report

        control = check()
        if control["status"] != "PASS":
            raise RuntimeError(f"aligned control failed: {control}")
        results = {}
        for name, file, before, after, expected in mutations:
            path = scratch / file
            source = path.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"review mutation site: {name}")
            path.write_text(source.replace(before, after), encoding="utf-8")
            report = check()
            detected = set(report["failures_by_property"])
            if report["status"] != "FAIL" or not expected <= detected:
                raise RuntimeError(f"{name}: expected {sorted(expected)}, observed {report}")
            results[name] = sorted(detected)
            path.write_text(source, encoding="utf-8")
        if check() != control:
            raise RuntimeError("restored control differs")
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", nargs=4, metavar=("ACTION", "PERIOD", "QUANTA", "DELIVERY"))
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    cases = list(product(ACTIONS, PERIODS, QUANTA, DELIVERY))
    if args.case:
        action, period, quanta, delivery = args.case
        if not quanta.isdecimal() or (action, period, int(quanta), delivery) not in cases:
            parser.error("expected pause/end, before/stopped, 0..75, immediate/queued")
        cases = [(action, period, int(quanta), delivery)]
    try:
        report = asyncio.run(search(cases))
        if args.mutation_check:
            mutations = mutation_check()
            if asyncio.run(search(cases)) != report:
                raise RuntimeError("restored search differs")
            report.update(mutations=mutations, restored_matches=True)
    except Exception as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        report = dict(status="ERROR", error=f"{type(error).__name__}: {detail}")
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return {"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
