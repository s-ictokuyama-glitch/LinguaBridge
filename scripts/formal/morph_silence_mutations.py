"""Break one classroom observation at a time in an isolated copy of the product."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
# All four changes break the passing control with 16 initial quiet frames and no burst.
MUTATIONS = (
    ("late_asr_segment", "audio/vad.py",
     "self._silence_run >= self._silence_frames_to_close", "self._silence_run > self._silence_frames_to_close",
     ("before_burst", "asr")),
    ("early_boundary", "audio/vad.py",
     "self._post_silence_run < self._force_silence_frames",
     "self._post_silence_run < self._force_silence_frames - 1",
     ("before_deadline", "teacher")),
    ("ignore_boundary", "pipeline.py",
     "else self._assembler.on_boundary()", "else None",
     ("after_silence", "teacher")),
    ("drop_following_chinese", "caption_delivery.py",
     "if self.roster.send(client, captions[selected]):",
     'if (selected != "zh" or job.utterance.seq != 2) and self.roster.send(client, captions[selected]):',
     ("following", "zh")),
)


def probe(script):
    child = subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", str(script), "--case", "16", "0"],
        cwd=script.parents[2], capture_output=True, encoding="utf-8", timeout=30,
    )
    if child.returncode not in (0, 1):
        raise RuntimeError(f"scratch run exited {child.returncode}: {child.stdout} {child.stderr}")
    report = json.loads(child.stdout)
    if report["status"] != ("PASS" if child.returncode == 0 else "FAIL"):
        raise RuntimeError(f"incomplete scratch run: {report}")
    return report


def check_mutations():
    with tempfile.TemporaryDirectory(prefix="morph-silence-") as temp:
        root = Path(temp)
        shutil.copytree(ROOT / "server", root / "server", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "config.yaml", root)
        scripts = root / "scripts/formal"
        scripts.mkdir(parents=True)
        for name in ("morph_silence.py", "lesson_audio_boundary.py", "lesson_liveness.py", "silero_contract.py"):
            shutil.copy2(ROOT / "scripts/formal" / name, scripts)

        script = scripts / "morph_silence.py"
        control = probe(script)
        results = {}
        for name, file, before, after, (phase, field) in MUTATIONS:
            if control["status"] != "PASS":
                results[name] = {"detected": False, "reason": "unmodified control failed", "case": control["case"]}
                continue
            path = root / "server" / file
            original = path.read_bytes()
            code = original.decode("utf-8").replace("\r\n", "\n")
            if code.count(before) != 1:
                raise ValueError(f"review {name} against {file}")
            try:
                path.write_text(code.replace(before, after), encoding="utf-8")
                broken = probe(script)
            finally:
                path.write_bytes(original)
            witness = next((d for d in broken["differences"] if (d["phase"], d["field"]) == (phase, field)), None)
            if broken["status"] != "FAIL" or witness is None:
                raise RuntimeError(f"{name} missed {phase}/{field}: {broken}")
            results[name] = {"detected": True, "case": control["case"], "difference": witness}

        if probe(script) != control:
            raise RuntimeError("restored scratch control differs")
        return results
