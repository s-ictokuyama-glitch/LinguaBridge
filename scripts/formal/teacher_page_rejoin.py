"""Check teacher re-entry from browser microphone input to two students' rendered captions."""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import AsyncExitStack
from dataclasses import dataclass
from importlib.metadata import version
from itertools import product
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

import numpy as np
from fastapi import FastAPI
from playwright.async_api import BrowserContext, Page, TimeoutError as BrowserTimeout, async_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from server import event_loop
from server.asr.base import ASRResult
from server.config import load_config
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from server.network import InterfaceAddress
from server.publication import Publication
from server.serving import ListenerSpec, start_listeners


PATHS = ("socket", "reload", "takeover")
STATES = ("live", "paused")
MEDIA = ("running", "suspended", "ended")
WORDS = ("水は液体です。", "氷は固体です。", "次は実験です。")
TONES = (400, 800, 1200)
LANGS = ("en", "zh")
ALL_CASES = [(route, state, media) for route, state, media in product(PATHS, STATES, MEDIA)
             if route == "socket" or media == "running"]
CARD_ROWS = """cards => cards.map(card => {
  const text = card.querySelector(".text");
  return [Number(card.dataset.seq), card.querySelector(".ja").textContent, text.lang, text.textContent];
})"""
CAPTURE = """() => ({ui: document.body.dataset.state, joined: state.joined,
  context: state.audioCtx?.state ?? null, track: state.micStream?.getAudioTracks()[0]?.readyState ?? null,
  startEnabled: !document.querySelector("#start-btn").disabled, voiced: probe.voiced, packets: probe.packets})"""


class ToneRecognition:
    def warmup(self):
        pass

    def transcribe(self, pcm, sample_rate):
        frequency = np.argmax(abs(np.fft.rfft(pcm))) * sample_rate / len(pcm)
        for tone, word in zip(TONES, WORDS, strict=True):
            if abs(tone - frequency) < 15:
                return ASRResult(word)
        raise ValueError(f"unexpected microphone signal at {frequency:.1f} Hz")


def microphone_inputs(real):
    if real:
        from lesson_audio_boundary_real import read_fixture
        pcm = [read_fixture(f"{i:02}") for i in range(1, 4)]
    else:
        t = np.arange(6400) / 16000
        pcm = [(8000 * np.sin(2 * np.pi * tone * t)).astype("<i2").tobytes() for tone in TONES]
    return [base64.b64encode(data).decode("ascii") for data in pcm]


@dataclass
class Classroom:
    app: FastAPI
    url: str
    context: BrowserContext
    students: dict[str, Page]
    inputs: list[str]
    real: bool
    barrier: int = 0

    async def teacher(self):
        teacher = await self.context.new_page()
        await teacher.goto(self.url + "/teacher")
        return teacher

    async def utterance(self, teacher, index):
        await teacher.evaluate("pcm => playLessonPCM(pcm)", self.inputs[index])
        await acknowledge_input(teacher)
        pipeline = self.app.state.pipeline
        await pipeline.drain(timeout=120 if self.real else 5)
        # FIFO markers follow all queued captions. DOM reads then see every card,
        # including unexpected duplicates or a caption sent while paused.
        self.barrier += 1
        pipeline.roster.broadcast(dict(type="probe_barrier", id=self.barrier))
        rows = {}
        for lang, student in self.students.items():
            await student.wait_for_function("id => probe.barrier === id", arg=self.barrier)
            rows[lang] = await student.locator("#cards .card").evaluate_all(CARD_ROWS)
        return rows

    def delivered(self, rows, count):
        if not self.real:
            return all(rows[lang] == [[i, word, lang, f"[{lang}] {word}"]
                                      for i, word in enumerate(WORDS[:count], 1)] for lang in LANGS)
        # Anchors identify the three fixture sentences across ASR wording variations.
        anchors = ("光合成", "二酸化炭素", "教科書")
        return (all(len(rows[lang]) == count and all(
            seq == i and anchors[i - 1] in ja and label == lang and text.strip()
            for i, (seq, ja, label, text) in enumerate(rows[lang], 1)) for lang in LANGS)
            and [r[:2] for r in rows["en"]] == [r[:2] for r in rows["zh"]])


async def acknowledge_input(teacher):
    # The product echoes this idempotent request on the same FIFO socket after
    # processing the preceding join, control or PCM. Recording is already off.
    await teacher.evaluate("""() => new Promise(resolve => {
      const ws = state.ws;
      function acknowledge(event) {
        if (JSON.parse(event.data).type !== "recording") return;
        ws.removeEventListener("message", acknowledge);
        resolve();
      }
      ws.addEventListener("message", acknowledge);
      ws.send(JSON.stringify({type:"recording", on:false}));
    })""")


async def exercise(room, case):
    route, prior, media = case
    teacher = await room.teacher()
    await teacher.wait_for_function("probe.joins === 1")
    await teacher.locator("#start-btn").click()
    await teacher.wait_for_function("document.body.dataset.state === 'live'")
    # Establish a working, clocked capture path before the control tone.
    # The first MediaStream render quantum can follow the "live" notification.
    await teacher.wait_for_function("probe.packets >= 3", timeout=5000)
    original = await room.utterance(teacher, 0)
    if not room.delivered(original, 1):
        capture = await teacher.evaluate(CAPTURE)
        raise RuntimeError(f"pre-handoff control failed for {case}: capture={capture}, cards={original}")
    if prior == "paused":
        await teacher.locator("#pause-btn").click()
        await teacher.wait_for_function("document.body.dataset.state === 'paused'")
    if media == "suspended":
        await teacher.evaluate("state.audioCtx.suspend()")
    elif media == "ended":
        await teacher.evaluate("state.micStream.getAudioTracks().forEach(track => track.stop())")

    old = teacher
    if route == "reload":
        await teacher.reload()
    elif route == "socket":
        await teacher.evaluate("state.ws.close()")
    else:
        teacher = await room.teacher()
    evidence = {"case": case}
    try:
        await teacher.wait_for_function("n => probe.joins >= n", arg=2 if route == "socket" else 1,
                                        timeout=7000)
        if route == "takeover":
            await old.wait_for_function("probe.closes.includes(4000)", timeout=3000)
        await acknowledge_input(teacher)
        await teacher.wait_for_function("s => document.body.dataset.state === s", arg=prior, timeout=3000)
    except BrowserTimeout:
        return dict(**evidence, violations=["rejoin_state"], observed=await teacher.evaluate(CAPTURE))
    evidence["rejoined"] = await teacher.evaluate(CAPTURE)
    failures = []
    if prior == "paused":
        paused = await room.utterance(teacher, 1)
        packets = (await teacher.evaluate(CAPTURE))["packets"] - evidence["rejoined"]["packets"]
        if paused != original or packets:
            failures.append("paused_quiet")
            evidence["while_paused"] = dict(packets=packets, cards=paused)
    # Use a single available Start/Resume gesture if input needs acquiring.
    # A live teacher should not have to stop the class just to enable the mic.
    state = evidence["rejoined"]
    if state["startEnabled"] and (prior == "paused" or state["context"] != "running" or state["track"] != "live"):
        await teacher.locator("#start-btn").click()
        try:
            await teacher.wait_for_function("document.body.dataset.state === 'live'", timeout=3000)
        except BrowserTimeout:
            failures.append("resume_state")
    if room.app.state.lesson.state != "live" and "resume_state" not in failures:
        failures.append("resume_state")
    phases = []
    for index in (1, 2):
        before = await teacher.evaluate(CAPTURE)
        rows = await room.utterance(teacher, index)
        after = await teacher.evaluate(CAPTURE)
        phases.append(dict(packets=after["packets"] - before["packets"],
                           voiced=after["voiced"] - before["voiced"], cards=rows))
        if not room.delivered(rows, index + 1):
            failures.append(("first_captions", "following_captions")[index - 1])
    return dict(**evidence, phases=phases, violations=failures)


async def run_case(browser, case, web_dir, real, models, inputs):
    config = load_config(ROOT / "config.yaml")
    if (config.turn.strategy, config.vad.engine, config.asr.engine, config.mt.engine,
            config.recording.default_on) != ("morph", "silero", "haas-onnx", "hy-mt2", False):
        raise ValueError("review the classroom bounds for the current configuration")
    if models:
        config.models.dir = str(models.resolve())
    config.mt.cache_size = 0
    engines = {}
    if not real:
        config.vad.engine = "energy"
        config.vad.threshold = 300
        engines = dict(asr_engine=ToneRecognition(), mt_engine=FakeTranslationEngine())
    nic = InterfaceAddress("fixture", "192.168.5.25", True, "physical")
    async with AsyncExitStack() as stack:
        stack.enter_context(patch("server.network.list_addresses", return_value=[nic]))
        stack.enter_context(patch("server.main.WEB_DIR", web_dir))
        app = create_app(config, Publication(config.server), **engines)
        listeners = await stack.enter_async_context(start_listeners(
            app, [ListenerSpec("127.0.0.1", 0, lifespan=True)], install_signals=False))
        await asyncio.wait_for(app.state.pipeline._warmup_task, 180)
        if not app.state.pipeline.ready:
            raise RuntimeError("model preparation failed")
        url = f"http://127.0.0.1:{listeners.ports[0]}"

        async def context():
            ctx = await browser.new_context()
            stack.push_async_callback(ctx.close)
            await ctx.add_init_script(path=Path(__file__).with_name("teacher_rejoin_media.js"))
            return ctx

        students = {}
        for lang in LANGS:
            student = await (await context()).new_page()
            await student.goto(f"{url}/?code={app.state.lesson.join_code}")
            await student.locator(f'#lang-options button[data-code="{lang}"]').click()
            await student.locator("#join-btn").click()
            await student.wait_for_function("probe.joins === 1")
            students[lang] = student
        room = Classroom(app, url, await context(), students, inputs, real)
        return await asyncio.wait_for(exercise(room, case), 240 if real else 40)


async def search(browser, cases, web_dir, real=False, models=None):
    inputs = microphone_inputs(real)
    rows = [await run_case(browser, case, web_dir, real, models, inputs) for case in cases]
    failed = sum(bool(row["violations"]) for row in rows)
    return dict(status="FAIL" if failed else "PASS", passed=len(rows) - failed, failed=failed, cases=rows)


def violations_by_case(report):
    return {tuple(row["case"]): row["violations"] for row in report["cases"]}


async def mutations(browser, baseline):
    # Each mutation starts from a passing case and takes effect after the first caption.
    changes = (
        ("paused_start_disabled", ("reload", "paused", "running"), "teacher.js",
         'el.startBtn.disabled = !connected || s === "live";',
         'el.startBtn.disabled = !connected || s === "live" || s === "paused";',
         {"resume_state", "first_captions", "following_captions"}),
        ("lost_third_card", ("socket", "live", "running"), "student.js",
         '  card.dataset.seq = msg.seq;', '  if (msg.seq === 3 && msg.lang === "zh") return;\n  card.dataset.seq = msg.seq;',
         {"following_captions"}),
        ("resume_manual_pause", ("reload", "paused", "running"), "teacher.js",
         '      applySessionState(msg.session_state);',
         '      applySessionState(msg.session_state);\n      if (msg.session_state === "paused") sendControl("start");',
         {"rejoin_state"}),
        ("send_while_paused", ("socket", "paused", "running"), "teacher.js",
         'state.sessionState === "live" &&', 'state.sessionState !== "ended" &&',
         {"paused_quiet"}),
    )
    detected = []
    with tempfile.TemporaryDirectory(prefix="teacher-page-rejoin-") as temp:
        web_dir = Path(temp) / "web"
        shutil.copytree(ROOT / "web", web_dir)
        for name, case, filename, before, after, expected in changes:
            file = web_dir / filename
            source = file.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"review mutation site for {name}")
            if violations_by_case(baseline)[case]:
                raise RuntimeError(f"unbroken control failed for {name}")
            file.write_text(source.replace(before, after), encoding="utf-8")
            broken = (await search(browser, [case], web_dir))["cases"][0]
            if set(broken["violations"]) != expected:
                raise RuntimeError(f"{name}: expected {sorted(expected)}, observed {broken}")
            shutil.copy2(ROOT / "web" / filename, file)
            detected.append(dict(name=name, case=case, violations=sorted(expected)))
        restored = await search(browser, ALL_CASES, web_dir)
        if violations_by_case(restored) != violations_by_case(baseline):
            raise RuntimeError("restored search changed which cases or properties failed")
    return detected


async def run(args):
    cases = [tuple(args.case)] if args.case else ALL_CASES
    if any(case not in ALL_CASES for case in cases):
        raise ValueError(f"choose a case from {ALL_CASES}")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True, args=[
            "--autoplay-policy=no-user-gesture-required", "--use-fake-device-for-media-stream",
        ])
        try:
            report = await search(browser, cases, ROOT / "web", args.real, args.models_dir)
            report.update(browser=browser.version, playwright=version("playwright"), real_models=args.real)
            if args.mutation_check:
                try:
                    report["mutations"] = await mutations(browser, report)
                    report["restored_matches"] = True
                except Exception as error:
                    report.update(search_status=report["status"], status="ERROR", error=str(error))
            return report
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", nargs=3, metavar=("ROUTE", "STATE", "MEDIA"))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--real", action="store_true")
    modes.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if args.case and args.mutation_check:
        parser.error("--mutation-check requires the full bounded search")
    if args.models_dir and not args.real:
        parser.error("--models-dir requires --real")
    try:
        report = event_loop.run(run(args))
    except Exception as error:
        report = dict(status="ERROR", error=f"{type(error).__name__}: {error}")
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    sys.exit({"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]])
