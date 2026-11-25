"""Check microphone sample clocks and replay the emitted PCM through a bilingual lesson."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
CAPTURE = Path(__file__).with_suffix(".cjs")
RATE = 44100


def read_report(command, *, request=None, timeout=120):
    child = subprocess.run(command, input=request, capture_output=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    if child.returncode not in (0, 1):
        raise RuntimeError(f"{command} exited {child.returncode}: {child.stdout} {child.stderr}")
    report = json.loads(child.stdout)
    if report["status"] != ("PASS", "FAIL")[child.returncode]:
        raise RuntimeError(f"inconsistent exit code and report: {report}")
    return report


def capture(root, **options):
    request = json.dumps(dict(root=str(root), **options))
    return read_report(["node", str(CAPTURE)], request=request)


def lesson(directory, seconds):
    from teacher_audio import replay

    pcm = directory / "lesson.pcm"
    wire = capture(ROOT, rate=RATE, seconds=seconds, output=str(pcm))
    if wire["status"] == "FAIL":
        return wire
    result = asyncio.run(replay(pcm, seconds))
    return dict(result, capture=wire)


def suite(directory):
    bounded = capture(ROOT)
    sustained = lesson(directory, 3000)
    passed = bounded["status"] == sustained["status"] == "PASS"
    return dict(status="PASS" if passed else "FAIL", bounded=bounded, lesson=sustained)


async def real_lesson(directory, models):
    import numpy as np
    from classroom_language_demand import start_lesson
    from lesson_audio_boundary_real import read_fixture
    from lesson_liveness import Socket
    from server.config import load_config
    from server.connection import Connection
    from server.main import create_app
    from server.publication import Publication

    config = load_config(ROOT / "config.yaml")
    if models:
        config.models.dir = str(models.resolve())
    if (config.vad.engine, config.turn.strategy, config.asr.engine, config.mt.engine) != (
            "silero", "morph", "haas-onnx", "hy-mt2"):
        raise ValueError("expected shipping Silero, morph, Haas ONNX and Hy-MT2")

    # Synthesize one continuous native-rate microphone stream from the recorded speech.
    fixtures = (("03", "教科書"), ("02", "光合成"), ("07", "質問"))
    chunks = []
    for name, _ in fixtures:
        recorded = np.frombuffer(read_fixture(name), dtype="<i2")
        chunks.extend((recorded, np.zeros(25600, dtype="<i2")))
    audio = np.concatenate(chunks)
    ticks = np.arange(len(audio) * RATE // 16000) * (16000 / RATE)
    source = directory / "microphone.f32"
    (np.interp(ticks, np.arange(len(audio)), audio) / 32768).astype("<f4").tofile(source)
    pcm = directory / "speech.pcm"
    wire = capture(ROOT, rate=RATE, input=str(source), output=str(pcm))
    if wire["status"] == "FAIL":
        return wire

    app = create_app(config, Publication(config.server))
    pipeline, live = app.state.pipeline, app.state.lesson
    teacher = Socket()
    connection = Connection(teacher, "127.0.0.1", live, config.limits)
    students = {lang: Socket() for lang in ("en", "zh")}
    try:
        await start_lesson(live, connection, 180)
        for lang, socket in students.items():
            pupil = Connection(socket, "127.0.0.1", live, config.limits)
            await pupil.receive(json.dumps(dict(type="join", role="student", code=live.join_code, lang=lang)))
        with pcm.open("rb") as stream:
            while packet := stream.read(3200):
                await connection.receive(packet)
                await asyncio.sleep(0)
        await pipeline.drain(timeout=180)
        await asyncio.wait_for(asyncio.gather(*(s._queue.join() for s in live.roster._senders.values())), 10)
        finals = [(m["seq"], m["ja"]) for m in teacher.frames if m["type"] == "asr_final"]
        results = {lang: [(m["seq"], m["ja"], m["lang"], m["text"])
                          for m in socket.frames if m["type"] == "caption"] for lang, socket in students.items()}
        history = pipeline.captions.history.since(0)
        checks = {
            "recordings_in_order": len(finals) == len(fixtures) and all(
                seq == index and word in ja
                for index, ((seq, ja), (_, word)) in enumerate(zip(finals, fixtures), 1)),
            "recognitions": pipeline.asr_calls == len(fixtures),
            "translations": pipeline.captions.metrics().calls == 2 * len(fixtures),
        }
        for lang, rows in results.items():
            saved = [(u.seq, u.text_ja, lang, u.translations[lang].text) for u in history if lang in u.translations]
            checks[lang] = (rows == saved and [(seq, ja) for seq, ja, _, _ in rows] == finals
                            and all(text.strip() for _, _, _, text in rows))
        return dict(status="PASS" if all(checks.values()) else "FAIL", checks=checks,
                    capture=wire, finals=finals, captions=results, asr_calls=pipeline.asr_calls,
                    mt_calls=pipeline.captions.metrics().calls)
    finally:
        await pipeline.stop()


# Run each variant against a healthy witness first. Only scratch product files are edited.
def mutation_check(directory):
    witness = dict(only=[44100, -1, 1])
    if capture(ROOT, **witness)["status"] != "PASS":
        raise RuntimeError("unhealthy control for microphone mutations")
    scratch = directory / "mutated"
    web = scratch / "web"
    web.mkdir(parents=True)
    shutil.copyfile(ROOT / "web/teacher.js", web / "teacher.js")
    worklet = web / "audio-worklet.js"
    healthy = (ROOT / "web" / worklet.name).read_text(encoding="utf-8")
    results = {}
    for name, before, after, expected in (
        ("round_ratio", "sampleRate / TARGET_RATE", "Math.round(sampleRate / TARGET_RATE)", "packet_count"),
        ("discard_fraction", "const frac = pos - i;", "const frac = 0;", "pcm_sample"),
        ("lose_position", "this.pos = pos - keepFrom;", "this.pos = 0;", "pcm_sample"),
        ("truncate_packet", "this.out.buffer.slice(0)", "this.out.buffer.slice(2)", "packet_bytes"),
    ):
        if healthy.count(before) != 1:
            raise RuntimeError(f"mutation site changed: {name}")
        worklet.write_text(healthy.replace(before, after), encoding="utf-8")
        broken = capture(scratch, **witness)
        violation = broken.get("firstViolation", {})
        if broken["status"] != "FAIL" or violation.get("property") != expected:
            raise RuntimeError(f"{name} did not detect {expected}: {broken}")
        results[name] = violation

    # A fresh interpreter imports the scratch server, including the mutated caption sender.
    shutil.copyfile(ROOT / "web/audio-worklet.js", worklet)
    shutil.copytree(ROOT / "server", scratch / "server", ignore=shutil.ignore_patterns("__pycache__"))
    formal = scratch / "scripts/formal"
    formal.mkdir(parents=True)
    for filename in (Path(__file__).name, CAPTURE.name, "teacher_audio.cjs", "teacher_audio.py",
                     "lesson_endurance.py", "lesson_liveness.py"):
        shutil.copyfile(CAPTURE.parent / filename, formal / filename)
    command = [sys.executable, "-X", "utf8", "-B", str(formal / Path(__file__).name), "--short-lesson"]
    if read_report(command)["status"] != "PASS":
        raise RuntimeError("unhealthy control for caption mutation")
    sender = scratch / "server/caption_delivery.py"
    source = sender.read_text(encoding="utf-8")
    before = "text=translated.text,"
    if source.count(before) != 1:
        raise RuntimeError("caption mutation site changed")
    sender.write_text(source.replace(before, 'text="broken" if selected == "zh" and job.utterance.seq == 2 '
                                     'else translated.text,'), encoding="utf-8")
    broken = read_report(command)
    violation = broken.get("violation", {})
    if broken["status"] != "FAIL" or violation.get("property") != "caption_delivery":
        raise RuntimeError(f"missed the second Chinese caption: {broken}")
    results["second_chinese_caption"] = violation
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--case", type=int, nargs=3, metavar=("RATE", "CUT1", "CUT2"))
    mode.add_argument("--short-lesson", action="store_true")
    mode.add_argument("--mutation-check", action="store_true")
    mode.add_argument("--real", action="store_true")
    parser.add_argument("--models-dir", type=Path)
    args = parser.parse_args()
    if args.models_dir and not args.real:
        parser.error("--models-dir requires --real")
    with tempfile.TemporaryDirectory(prefix="microphone-rates-") as temp:
        directory = Path(temp)
        if args.case:
            return capture(ROOT, only=args.case)
        if args.short_lesson:
            return lesson(directory, 10)
        if args.real:
            return asyncio.run(real_lesson(directory, args.models_dir))
        result = suite(directory)
        if args.mutation_check and result["status"] == "PASS":
            mutations = mutation_check(directory)
            if suite(directory) != result:
                raise RuntimeError("original source did not reproduce the full baseline after mutations")
            result.update(mutations=mutations, restored_matches=True)
        return result


if __name__ == "__main__":
    try:
        report = main()
    except Exception as error:
        report = dict(status="ERROR", error=f"{type(error).__name__}: {error}")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    sys.exit({"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]])
