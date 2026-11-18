"""Trace paused microphone speech through the teacher Worklet into the shipped ASR and MT."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import sys

from lesson_audio_boundary_real import ROOT, read_fixture, replay, sample_offset
from server.config import load_config


def capture(action, fixtures):
    request = {name: base64.b64encode(pcm).decode("ascii") for name, pcm in fixtures.items()}
    child = subprocess.run(
        ["node", str(ROOT / "scripts/formal/teacher_audio_resume_speech.cjs")],
        cwd=ROOT, input=json.dumps({"action": action, "fixtures": request}),
        capture_output=True, encoding="utf-8", check=False, timeout=30,
    )
    if child.returncode:
        raise RuntimeError(f"teacher capture failed: {child.stderr}")
    captures = json.loads(child.stdout)
    return {case: {phase: base64.b64decode(data, validate=True) for phase, data in phases.items()}
            for case, phases in captures.items()}


def translated(row, seq):
    return ([entry[0] for entry in row["finals"]] == [seq]
            and bool(row["finals"][0][1].strip()) and row["delivered"] and row["mt_calls"] == 2)


async def check(action, models_dir):
    config = load_config(ROOT / "config.yaml")
    if models_dir:
        config.models.dir = str(models_dir.resolve())
    if (config.vad.engine, config.turn.strategy, config.asr.engine, config.mt.engine) != (
            "silero", "morph", "haas-onnx", "hy-mt2"):
        raise ValueError("requires the shipped Silero, morph, Haas ONNX and Hy-MT2 configuration")
    config.mt.cache_size = 0
    # The old microphone contributes 64 ms, less than one 100 ms network packet.
    # Cut 160 ms of leading quiet from the resumed fixture to put speech next to the boundary.
    fixtures = {"inactive": read_fixture("01")[2 * 6000:2 * 7024],
                "first": read_fixture("02")[2 * 2560:], "following": read_fixture("03")}
    marker = fixtures["inactive"]
    if len(marker) != 2048 or not any(marker) or any(
            sample_offset(fixtures[phase], marker) >= 0 for phase in ("first", "following")):
        raise ValueError("expected a unique 1024-sample marker in the stopped microphone input")
    captured = capture(action, fixtures)
    wire = {}
    lessons = {}
    for case in ("control", "spoken"):
        pcm = captured[case]
        wire[case] = {phase: {"packets": len(data) // 3200, "sha256": hashlib.sha256(data).hexdigest(),
                              "inactive_sample_offset": sample_offset(data, marker)}
                      for phase, data in pcm.items()}
        if case == "control" and any(row["inactive_sample_offset"] >= 0 for row in wire[case].values()):
            raise RuntimeError("inactive marker occurred on the wire in the silent control")
        lessons[case] = await replay(config, b"", [marker], pcm, action)
        if case == "control" and not all(translated(lessons[case][p], seq)
                                         for seq, p in enumerate(("first", "following"), 1)):
            raise RuntimeError(f"silent control did not produce both bilingual turns: {lessons[case]}")

    properties = {}
    for seq, phase in enumerate(("first", "following"), 1):
        before = lessons["control"][phase]
        after = lessons["spoken"][phase]
        properties[phase + "_wire_isolated"] = wire["control"][phase]["sha256"] == wire["spoken"][phase]["sha256"]
        properties[phase + "_asr_isolated"] = after["asr"] == before["asr"]
        properties[phase + "_translated"] = translated(after, seq)
        properties[phase + "_final_matches_control"] = after["finals"] == before["finals"]
        for lang in ("en", "zh"):
            properties[f"{phase}_{lang}_caption_matches_control"] = (
                after["captions"][lang] == before["captions"][lang]
            )
    return {"status": "PASS" if all(properties.values()) else "FAIL", "action": action,
            "properties": properties, "wire": wire, "lessons": lessons,
            "input_sha256": {k: hashlib.sha256(v).hexdigest() for k, v in fixtures.items()},
            "versions": {pkg: version(pkg) for pkg in ("onnxruntime", "llama-cpp-python", "faster-whisper")}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("pause", "end"), default="pause")
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    try:
        report = asyncio.run(check(args.action, args.models_dir))
    except Exception as error:
        report = {"status": "ERROR", "error": f"{type(error).__name__}: {error}"}
    output = json.dumps(report, indent=2, ensure_ascii=False)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    sys.exit({"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]])
