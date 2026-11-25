"""Confirm the attendance checks against isolated, one-at-a-time edits to the product."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
MUTATIONS = (
    ("skip_empty_class", "pipeline", "EEZ 000 111 asr", "model_checkpoint",
     "self.asr_calls += 1", "if not self.roster.students():\n            return\n        self.asr_calls += 1"),
    ("recognize_twice", "pipeline", "EEZ 111 111 mt0", "asr_calls",
     "self.asr_calls += 1", "self.asr_calls += 1\n        await self._asr_runner.run(\n"
     "            partial(self._asr.transcribe, segment.pcm, self._segmenter.sample_rate),\n"
     "            timeout=self._limits.asr_timeout_s)"),
    ("unused_language", "caption_delivery", "EEZ 100 100 asr", "translations_per_language",
     "for lang, ids in sorted(recipients.items()):",
     'recipients.setdefault("zh", [])\n        for lang, ids in sorted(recipients.items()):'),
    ("only_first_recipient", "caption_delivery", "EEZ 111 111 mt0", "student_captions",
     "closed_at, tuple(ids)", "closed_at, tuple(ids[:1])"),
    ("send_to_late_joiner", "caption_delivery", "EEZ 101 011 mt0", "student_captions",
     "for client_id in pending:", "for client_id in [c.id for c in self.roster.students() if c.lang == lang]:"),
    ("wrong_final", "pipeline", "EEZ 111 111 mt0", "teacher_finals",
     "seq=utterance.seq,", "seq=utterance.seq + 1,"),
    ("stop_after_history_window", "caption_delivery", "lesson", "student_captions",
     "if self.roster.send(client, captions[selected]):",
     'if (selected != "zh" or job.utterance.seq <= 50) and self.roster.send(client, captions[selected]):'),
)


def probe(root, case):
    args = ["--lesson"] if case == "lesson" else ["--case", *case.split()]
    child = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-B", str(root / "scripts/formal/classroom_language_demand.py"), *args],
        cwd=root, capture_output=True, encoding="utf-8", timeout=100,
    )
    if child.returncode not in (0, 1):
        raise RuntimeError(f"scratch run exited {child.returncode}: {child.stdout} {child.stderr}")
    report = json.loads(child.stdout)
    if report["status"] != ("PASS" if child.returncode == 0 else "FAIL"):
        raise RuntimeError(f"unexpected scratch exit status: {report}")
    return report


def check_mutations():
    results = {}
    with tempfile.TemporaryDirectory(prefix="classroom-demand-") as directory:
        scratch = Path(directory)
        shutil.copytree(ROOT / "server", scratch / "server", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "config.yaml", scratch)
        scripts = scratch / "scripts/formal"
        scripts.mkdir(parents=True)
        for file in ("classroom_language_demand.py", "lesson_liveness.py"):
            shutil.copy2(ROOT / "scripts/formal" / file, scripts)
        for name, module, case, prop, before, after in MUTATIONS:
            control = probe(scratch, case)
            if control["status"] != "PASS":
                results[name] = {"detected": False, "control": control}
                continue
            target = scratch / "server" / f"{module}.py"
            original = target.read_bytes()
            source = target.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise ValueError(f"review mutation {name} in {module}.py")
            try:
                target.write_text(source.replace(before, after), encoding="utf-8")
                broken = probe(scratch, case)
            finally:
                target.write_bytes(original)
            witness = broken.get("lesson_counterexample") or broken["first_counterexamples"].get(prop)
            if broken["status"] != "FAIL" or not witness or witness["property"] != prop:
                raise RuntimeError(f"{name} missed {prop}: {broken}")
            results[name] = {"detected": True, **witness}
    return results
