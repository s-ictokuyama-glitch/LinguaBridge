"""Explore live PCM arrivals while bilingual MT holds the ASR worker in publish()."""
from __future__ import annotations

import argparse
import asyncio
from itertools import product
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.formal.lesson_liveness import ControlledExecutor, Socket, Translation, Violation, callbacks
from server.asr.base import ASRResult
from server.config import load_config
from server.inference import InferenceRunner, InferenceSucceeded
from server.pipeline import Pipeline
from server.roster import Roster

LANGUAGES = ("en", "zh")
SILENCE = bytes(3200)


def sentence(identity, boundary):
    if identity == 2 and boundary == "silence":
        return "条件2が異なるので"
    return f"授業の発話{identity}です。"


class Recognition:
    def __init__(self, boundary, segment_seconds):
        self.boundary = boundary
        self.segment_seconds = segment_seconds

    def warmup(self):
        pass

    def transcribe(self, pcm, sample_rate):
        marker, = np.unique(pcm[pcm != 0])
        if len(pcm) != sample_rate * self.segment_seconds or sample_rate != 16000:
            raise ValueError(f"unexpected ASR segment: {len(pcm)} samples at {sample_rate}")
        return ASRResult(sentence(int(marker) - 1000, self.boundary))


class ImmediateASR:
    async def run(self, operation, *, timeout):
        return InferenceSucceeded(operation(), 0)

    async def aclose(self):
        pass


async def classroom(config, mt_capacity, boundary, gaps, *, shipping=False):
    config = config.model_copy(deep=True)
    if not shipping:
        config.limits.mt_queue_max = mt_capacity
        config.limits.mt_replay_watermark = 1
        config.limits.asr_queue_seconds = 2
    config.vad.engine = "energy"
    config.vad.threshold = 300
    segment_seconds = 3 if shipping else 1
    mt = ControlledExecutor()
    roster = Roster(config.limits)
    pipeline = Pipeline(roster, config, Recognition(boundary, segment_seconds), Translation(),
                        asr_runner=ImmediateASR(), mt_runner=InferenceRunner(mt))
    teacher = Socket()
    students = {lang: Socket() for lang in LANGUAGES}
    admitted, dropped, steps = [], [], []
    completed = 0

    def equal(name, expected, actual):
        if actual != expected:
            raise Violation(name, expected, actual, steps)

    def occupied_audio():
        # Each complete publication needs two MT slots. One extra slot belongs
        # to the running translation. Every explicit completion frees one slot.
        published = (completed + 1 + mt_capacity) // 2
        pending = admitted[published:]
        # For a clause, ASR releases the Segment before the zero-sample
        # TurnBoundary publishes it. A terminal sentence publishes in Segment.
        if pending and pending[0] == 2 and boundary == "silence":
            pending = pending[1:]
        return len(pending) * segment_seconds

    async def advance_mt(count):
        nonlocal completed
        for _ in range(count):
            identity = admitted[completed // 2]
            expected = (sentence(identity, boundary), LANGUAGES[completed % 2])
            equal("next_translation", expected, mt.current.key if mt.current else None)
            mt.complete()
            completed += 1
            await callbacks()

    async def feed_block(identity, quiet_ticks):
        speech = np.full(1600, 1000 + identity, dtype="<i2").tobytes()
        voice_ticks = 10 * segment_seconds - 7
        for packet in [SILENCE] * 2 + [speech] * voice_ticks + [SILENCE] * quiet_ticks:
            await pipeline.feed_audio(packet)

    async def offer(identity, releases, quiet_ticks):
        steps.append(f"complete {releases}, feed {identity}")
        await advance_mt(releases)
        if occupied_audio() + segment_seconds > config.limits.asr_queue_seconds:
            dropped.append(identity)
        else:
            admitted.append(identity)
        # The 200 ms pre-roll, voice and 500 ms closing silence determine
        # the segment size independently of production queue accounting.
        feed = asyncio.create_task(feed_block(identity, quiet_ticks))
        try:
            await callbacks()
            equal("audio_ingress", "returned", "returned" if feed.done() else "still waiting")
            await feed
        finally:
            if not feed.done():
                feed.cancel()
                await asyncio.gather(feed, return_exceptions=True)
        equal("dropped_audio", (len(dropped), len(dropped) * segment_seconds),
              (pipeline.asr_dropped_segments, pipeline.asr_dropped_seconds))
        equal("charged_audio", occupied_audio(), pipeline.audio_queue_seconds)
        remaining_jobs = 2 * len(admitted) - completed
        equal("mt_waiting", min(mt_capacity, max(remaining_jobs - 1, 0)), pipeline.captions.queue_depth)
        notices = {frame["code"] for frame in teacher.frames if frame["type"] == "error"}
        equal("audio_drop_notice", bool(dropped), "audio_dropped" in notices)
        if remaining_jobs > mt_capacity + 1:
            equal("mt_backlog_notice", True, "mt_backlog" in notices)

    async def drain():
        await advance_mt(2 * len(admitted) - completed)
        await pipeline.drain(timeout=5)
        await asyncio.wait_for(asyncio.gather(*(s._queue.join() for s in roster._senders.values())), 5)
        equal("drained", (0, 0), (pipeline.queue_depth, pipeline.audio_queue_seconds))
        finals = [(seq, sentence(identity, boundary)) for seq, identity in enumerate(admitted, 1)]
        equal("teacher_finals", finals, [(m["seq"], m["ja"]) for m in teacher.frames if m["type"] == "asr_final"])
        for lang, socket in students.items():
            expected = [(seq, ja, lang, f"{lang}:{ja}") for seq, ja in finals]
            equal("captions_" + lang, expected, [(m["seq"], m["ja"], m["lang"], m["text"])
                  for m in socket.frames if m["type"] == "caption"])
        equal("connections", [None] * 3, [s.closed for s in (teacher, *students.values())])

    try:
        await roster.add(teacher, "teacher")
        for lang, socket in students.items():
            await roster.add(socket, "student", lang)
        await pipeline.start()
        await asyncio.wait_for(pipeline._warmup_task, 5)
        if not pipeline.ready:
            raise RuntimeError("warmup failed")
        await pipeline.set_accepting_audio(True)
        if shipping:
            for identity in range(1, 601):
                service = 1 if identity <= 300 else 4
                releases = min(service, 2 * len(admitted) - completed)
                await offer(identity, releases, 25)
        else:
            for identity, releases in enumerate((0, 0, *gaps), 1):
                await offer(identity, releases, 10)
        await drain()
        steps.append("after drain")
        await offer(len(admitted) + len(dropped) + 1, 0, 10)
        await drain()
    finally:
        await pipeline.stop()
    return dict(captions_per_language=len(admitted), dropped=dropped)


async def check(cases, include_shipping):
    config = load_config(ROOT / "config.yaml")
    if (config.turn.strategy, config.turn.classifier, config.partial.enabled,
        config.limits.asr_queue_seconds, config.limits.mt_queue_max) != ("morph", "surface", False, 30, 256):
        raise ValueError("review the bounds and PCM recipe against the shipped configuration")
    passed = with_drops = failed = 0
    findings = {}
    shipping_result = None
    schedules = [(q, b, gaps, False) for q, b, gaps in cases]
    if include_shipping:
        schedules.append((config.limits.mt_queue_max, "terminal", (), True))
    for capacity, boundary, gaps, shipping in schedules:
        try:
            result = await asyncio.wait_for(classroom(config, capacity, boundary, gaps, shipping=shipping),
                                            timeout=60 if shipping else 8)
        except Violation as finding:
            failed += 1
            findings.setdefault(finding.detail["property"],
                                dict(capacity=capacity, boundary=boundary, gaps=gaps, **finding.detail))
        else:
            passed += 1
            with_drops += bool(result["dropped"])
            if shipping:
                shipping_result = result
    return dict(status="FAIL" if failed else "PASS", passed=passed, failed=failed,
                passing_with_drops=with_drops, shipping=shipping_result, first_counterexamples=list(findings.values()))


MUTATIONS = (
    ("block_teacher_input", "pipeline.py", "            await self._enqueue_asr(event)\n",
     "            await self._enqueue_asr(event)\n"
     "            if self.captions.queue_depth == self._limits.mt_queue_max:\n"
     "                await self.captions.drain(None)\n", "audio_ingress"),
    ("lose_full_mt_job", "caption_delivery.py", "await self._mt_queue.put(entry)",
     "if job.utterance.seq > 1:\n                    return False\n                await self._mt_queue.put(entry)",
     "charged_audio"),
    ("release_active_early", "asr_backlog.py", "self._waiting_samples + self._active_samples",
     "self._waiting_samples", "dropped_audio"),
    ("suppress_drop_notice", "pipeline.py", "if outcome is Dropped and isinstance(event, Segment):",
     "if False:", "audio_drop_notice"),
    ("suppress_mt_notice", "pipeline.py", "        last = self._notice_at.get(code)",
     '        if code == "mt_backlog":\n            return\n        last = self._notice_at.get(code)',
     "mt_backlog_notice"),
    ("lose_resumed_chinese", "caption_delivery.py", "if self.roster.send(client, captions[selected]):",
     'if (selected != "zh" or job.utterance.seq < 4) and self.roster.send(client, captions[selected]):',
     "captions_zh"),
)


def mutations():
    results = {}
    with tempfile.TemporaryDirectory(prefix="translation-pressure-") as directory:
        scratch = Path(directory)
        shutil.copytree(ROOT / "server", scratch / "server", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "config.yaml", scratch)
        scripts = scratch / "scripts/formal"
        scripts.mkdir(parents=True)
        for path in (Path(__file__), ROOT / "scripts/formal/lesson_liveness.py"):
            shutil.copy2(path, scripts)
        probe = [sys.executable, "-I", "-X", "utf8", "-B", str(scripts / Path(__file__).name),
                 "--case", "1", "terminal", "0000"]
        for name, file, before, after, expected in MUTATIONS:
            target = scratch / "server" / file
            source = target.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise ValueError(f"review mutation {name} in {file}")
            target.write_text(source.replace(before, after), encoding="utf-8")
            child = subprocess.run(probe, cwd=scratch, capture_output=True, encoding="utf-8", timeout=30)
            target.write_text(source, encoding="utf-8")
            if child.returncode != 1:
                raise RuntimeError(f"{name}: expected FAIL, got {child.stdout} {child.stderr}")
            finding, = json.loads(child.stdout)["first_counterexamples"]
            if finding["property"] != expected:
                raise RuntimeError(f"{name}: expected {expected}, got {finding}")
            results[name] = finding
        subprocess.run(probe, cwd=scratch, capture_output=True, check=True, timeout=30)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--case", nargs=3, metavar=("MT_SLOTS", "BOUNDARY", "GAPS"))
    group.add_argument("--shipping", action="store_true")
    group.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    cases = list(product((1, 2), ("terminal", "silence"), product(range(3), repeat=4)))
    if args.case:
        cases = [(q, b, gaps) for q, b, gaps in cases if [str(q), b, "".join(map(str, gaps))] == args.case]
        if not cases:
            parser.error("expected capacity 1 or 2, terminal or silence, and four digits from 0 to 2")
    if args.shipping:
        cases = []
    logging.disable(logging.CRITICAL)
    try:
        result = asyncio.run(check(cases, not args.case))
        if args.mutation_check and result["status"] == "PASS":
            broken = mutations()
            if asyncio.run(check(cases, True)) != result:
                raise RuntimeError("restored search differs from control")
            result.update(mutations=broken, restored_matches=True)
    except Exception as error:
        result = dict(status="ERROR", error=f"{type(error).__name__}: {error}")
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return {"PASS": 0, "FAIL": 1, "ERROR": 2}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
