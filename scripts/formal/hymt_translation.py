"""Check which live captions survive a failed Hy-MT2 completion and its recovery."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from itertools import product
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.formal.lesson_liveness import Socket
from server.asr.base import ASRResult
from server.config import load_config
from server.mt.hymt_engine import HyMt2Engine
from server.pipeline import Pipeline
from server.roster import Roster


SENTENCES = {"A": "水は液体です。", "B": "氷は固体です。", "C": "次は実験です。"}
TRANSLATIONS = {
    "en": {"A": "Water is a liquid.", "B": "Ice is a solid.", "C": "Next is an experiment."},
    "zh": {"A": "水是液体。", "B": "冰是固体。", "C": "接下来做实验。"},
}
OUTCOMES = ("ok", "error", "empty", "whitespace")
FOLLOWING = ("AA", "AB", "BA", "BB")
CACHES = ("on", "off")
SOURCES = tuple(SENTENCES)
PCM = {key: np.full(1600, 1001 + i, dtype="<i2").tobytes() for i, key in enumerate(SOURCES)}
SILENCE = bytes(3200)


@dataclass(frozen=True)
class Case:
    en: str
    zh: str
    following: str
    cache: str


class Recognition:
    def warmup(self):
        pass

    def transcribe(self, pcm, sample_rate):
        marker, = np.unique(pcm[pcm != 0])
        return ASRResult(SENTENCES[SOURCES[int(marker) - 1001]])


class ChatBackend:
    """The native boundary answers the language and source in the actual request."""

    metadata = {"tokenizer.chat_template": "fixture"}

    def __init__(self):
        self.outcomes = {}
        self.calls = []

    def tokenize(self, data, **kwargs):
        return list(data)

    def create_chat_completion(self, *, messages, **kwargs):
        prompt = messages[0]["content"]
        if prompt.endswith("こんにちは。"):  # product warmup
            text = "Hello."
        else:
            lang, = [lang for lang, label in (("en", "English"), ("zh", "Simplified Chinese"))
                     if label in prompt]
            key, = [key for key, source in SENTENCES.items() if prompt.endswith(source)]
            outcome = self.outcomes.get(lang, "ok")
            self.calls.append((key, lang, outcome))
            if outcome == "error":
                raise RuntimeError("injected generation failure")
            text = {"ok": TRANSLATIONS[lang][key], "empty": "", "whitespace": " \t\n "}[outcome]
        return {"choices": [{"message": {"content": text}}]}


async def feed_word(pipeline, key):
    # 200 ms voice opens a morph segment; 1 s silence closes it and the turn.
    for frame in (PCM[key],) * 2 + (SILENCE,) * 10:
        await pipeline.feed_audio(frame)


def captions(socket):
    return [(f["seq"], f["ja"], f["lang"], f["text"]) for f in socket.frames
            if f["type"] == "caption"]


async def run_case(case, config, gguf):
    config = config.model_copy(deep=True)
    if case.cache == "off":
        config.mt.cache_size = 0
    backend = ChatBackend()
    settings = config.mt.hy_mt2
    mt = HyMt2Engine(gguf, settings.threads, settings.temperature, settings.max_tokens_cap)
    teacher = Socket()
    students = {lang: Socket() for lang in TRANSLATIONS}
    roster = Roster(config.limits)
    pipeline = Pipeline(roster, config, Recognition(), mt)
    findings = []
    words = "A" + case.following + "C"

    def compare(name, receiver, expected, actual, fed):
        if expected != actual:
            findings.append(dict(property=name, receiver=receiver, case=asdict(case),
                                 pcm=fed, calls=list(backend.calls), expected=expected, actual=actual))

    async def drain():
        await pipeline.drain(timeout=5)
        sends = [sender._queue.join() for sender in roster._senders.values()]
        await asyncio.wait_for(asyncio.gather(*sends), timeout=5)

    with (patch.dict(sys.modules, llama_cpp=SimpleNamespace(Llama=lambda **kwargs: backend)),
          patch("server.mt.hymt_engine.load_cpu_backends")):
        try:
            await roster.add(teacher, "teacher")
            for lang, socket in students.items():
                await roster.add(socket, "student", lang)
            await pipeline.start()
            await asyncio.wait_for(pipeline._warmup_task, timeout=5)
            if not pipeline.ready:
                raise RuntimeError("fixture warmup failed")
            await pipeline.set_accepting_audio(True)
            backend.outcomes = {"en": case.en, "zh": case.zh}
            await feed_word(pipeline, "A")
            await drain()
            attempted = sorted({(key, lang) for key, lang, _ in backend.calls})
            compare("initial_requests", "model", [("A", "en"), ("A", "zh")], attempted, "A")
            offsets = {}
            for lang, socket in students.items():
                expected = []
                if backend.outcomes[lang] == "ok":
                    expected.append((1, SENTENCES["A"], lang, TRANSLATIONS[lang]["A"]))
                received = captions(socket)
                compare("first_caption", lang, expected, received, "A")
                offsets[lang] = len(received)

            backend.outcomes.clear()
            for key in words[1:]:
                await feed_word(pipeline, key)
            await drain()
            finals = [(f["seq"], f["ja"]) for f in teacher.frames if f["type"] == "asr_final"]
            compare("recognized_pcm", "teacher",
                    [(seq, SENTENCES[key]) for seq, key in enumerate(words, 1)], finals, words)
            for lang, socket in students.items():
                expected = [(seq, SENTENCES[key], lang, TRANSLATIONS[lang][key])
                            for seq, key in enumerate(words[1:], 2)]
                compare("after_recovery", lang, expected, captions(socket)[offsets[lang]:], words)
        finally:
            await pipeline.stop()
    return findings


async def check(cases):
    config = load_config(ROOT / "config.yaml")
    if (config.mt.engine != "hy-mt2" or config.turn.strategy != "morph"
            or config.mt.cache_size <= 0 or not {"en", "zh"}.issubset(config.language_codes)):
        raise ValueError("review the bounds for the current shipping configuration")
    config.vad.engine = "energy"
    config.vad.threshold = 300
    violations = {}
    counts = {}
    passed = failed = 0
    with tempfile.TemporaryDirectory(prefix="hymt-translation-") as directory:
        gguf = Path(directory) / "fixture.gguf"
        gguf.touch()
        for case in cases:
            findings = await run_case(case, config, gguf)
            if findings:
                failed += 1
            else:
                passed += 1
            for name in sorted({finding["property"] for finding in findings}):
                counts[name] = counts.get(name, 0) + 1
            for finding in findings:
                violations.setdefault(finding["property"], finding)
    return dict(status="FAIL" if failed else "PASS", passed=passed, failed=failed,
                failing_cases_by_property=counts, first_counterexamples=list(violations.values()))


def all_cases():
    return (Case(*values) for values in product(OUTCOMES, OUTCOMES, FOLLOWING, CACHES))


def mutation_check():
    case = Case("ok", "error", "AB", "on")
    if asyncio.run(check([case]))["status"] != "PASS":
        raise RuntimeError("mutation control must pass: --case ok error AB on")
    mutations = (
        ("wrong_language", "server/mt/hymt_engine.py",
         '"zh": "Simplified Chinese"', '"zh": "English"', ("initial_requests", "first_caption")),
        ("cache_error", "server/caption_delivery.py",
         "            if isinstance(outcome, InferenceFailed):",
         '            if isinstance(outcome, InferenceFailed):\n'
         '                self._mt_cache.put(self._cache_key(utterance.text_ja, lang), "")',
         ("after_recovery",)),
        ("wrong_final_seq", "server/pipeline.py",
         "                seq=utterance.seq,", "                seq=utterance.seq + 1,",
         ("recognized_pcm",)),
    )
    results = {}
    with tempfile.TemporaryDirectory(prefix="hymt-mutations-") as directory:
        scratch = Path(directory)
        shutil.copytree(ROOT / "server", scratch / "server",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(ROOT / "config.yaml", scratch)
        scripts = scratch / "scripts/formal"
        scripts.mkdir(parents=True)
        for filename in ("lesson_liveness.py", "hymt_translation.py"):
            shutil.copy2(ROOT / "scripts/formal" / filename, scripts)
        for name, relative, before, after, properties in mutations:
            target = scratch / relative
            source = target.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"review mutation site for {name}: {relative}")
            target.write_text(source.replace(before, after), encoding="utf-8")
            child = subprocess.run(
                [sys.executable, "-I", "-X", "utf8", "-B", str(scripts / "hymt_translation.py"),
                 "--case", case.en, case.zh, case.following, case.cache],
                cwd=scratch, capture_output=True, encoding="utf-8", timeout=30,
            )
            target.write_text(source, encoding="utf-8")
            if child.returncode != 1:
                raise RuntimeError(f"{name}: expected a counterexample: {child.stdout} {child.stderr}")
            report = json.loads(child.stdout)
            found = {v["property"] for v in report["first_counterexamples"]}
            if report["status"] != "FAIL" or not set(properties).issubset(found):
                raise RuntimeError(f"{name}: expected {properties}, detected {sorted(found)}")
            results[name] = sorted(found)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", nargs=4, metavar=("EN", "ZH", "FOLLOWING", "CACHE"))
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    cases = all_cases()
    if args.case:
        en, zh, words, cache = args.case
        if en not in OUTCOMES or zh not in OUTCOMES or words not in FOLLOWING or cache not in CACHES:
            parser.error(f"expected two of {OUTCOMES}, one of {FOLLOWING}, and one of {CACHES}")
        if args.mutation_check:
            parser.error("use --mutation-check with the full search")
        cases = [Case(*args.case)]
    logging.disable(logging.CRITICAL)
    report = {}
    try:
        report = asyncio.run(check(cases))
        if args.mutation_check:
            results = mutation_check()
            if asyncio.run(check(all_cases())) != report:
                raise RuntimeError("restored counterexamples differ from the original search")
            report.update(mutations=results, restored_matches=True)
    except Exception as error:
        if "status" in report:
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
