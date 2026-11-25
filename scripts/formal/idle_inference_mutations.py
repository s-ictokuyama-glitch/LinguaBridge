"""Check the idle-exit oracle against isolated changes to the real workers."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
MUTATIONS = (
    ("wrong_teacher_seq", "pipeline", "none", "next.teacher",
     "seq=utterance.seq,", "seq=utterance.seq + int(utterance.seq == 2),"),
    ("missing_chinese", "caption_delivery", "none", "next.zh",
     "if self.roster.send(client, captions[selected]):",
     'if (selected != "zh" or job.utterance.seq != 2) and self.roster.send(client, captions[selected]):'),
    ("no_asr_replacement", "inference_process", "asr", "following_bilingual",
     "if self._generation is None:\n            # Windows",
     "if self._generation is None:\n"
     "            if self._request_id and self._name == 'asr':\n"
     "                raise RuntimeError('replacement disabled')\n            # Windows"),
)


def probe(script, fault):
    child = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-B", str(script), "--case", fault, "B", "on"],
        cwd=script.parents[2], capture_output=True, encoding="utf-8", timeout=90,
    )
    if child.returncode not in (0, 1):
        raise RuntimeError(f"scratch process exited {child.returncode}: {child.stdout} {child.stderr}")
    report = json.loads(child.stdout)
    if report["status"] != ("PASS", "FAIL")[child.returncode]:
        raise RuntimeError(f"inconsistent scratch result: {report}")
    return report["cases"][0]


def check_mutations():
    results = {}
    with tempfile.TemporaryDirectory(prefix="idle-inference-") as directory:
        scratch = Path(directory)
        shutil.copytree(ROOT / "server", scratch / "server", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "config.yaml", scratch)
        formal = scratch / "scripts/formal"
        formal.mkdir(parents=True)
        for file in ("idle_inference_recovery.py", "classroom_language_demand.py", "lesson_liveness.py"):
            shutil.copy2(ROOT / "scripts/formal" / file, formal)
        script = formal / "idle_inference_recovery.py"
        for name, module, fault, property_name, before, after in MUTATIONS:
            control = probe(script, fault)
            if property_name in control["violations"]:
                raise RuntimeError(f"mutation {name} requires a passing control for {property_name}: {control}")
            target = scratch / "server" / f"{module}.py"
            original = target.read_bytes()
            source = target.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"review mutation {name} in {target.name}")
            try:
                target.write_text(source.replace(before, after), encoding="utf-8")
                broken = probe(script, fault)
            finally:
                target.write_bytes(original)
            if property_name not in broken["violations"] or probe(script, fault) != control:
                raise RuntimeError(f"{name}: missing violation or baseline mismatch after restoration")
            results[name] = dict(property=property_name, **broken["violations"][property_name])
    return results
