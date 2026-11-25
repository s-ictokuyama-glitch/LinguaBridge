"""Exercise the live-gap oracle against isolated edits to production sources."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
# First gap inside the first clause (packet 4) or after its ASR (packet 16).
MUTATIONS = (
    ("forget_audio", "pipeline.py", 4, "voiced_pcm",
     "if not self._mic_silent_warned:\n",
     "if not self._mic_silent_warned:\n            self._segmenter.reset()\n"),
    ("invent_quiet_pcm", "pipeline.py", 4, "asr_pcm",
     "if not self._mic_silent_warned:\n",
     "if not self._mic_silent_warned:\n            await self.feed_audio(bytes(1024))\n"),
    ("forget_pending_clause", "pipeline.py", 16, "teacher_finals",
     "if not self._mic_silent_warned:\n",
     "if not self._mic_silent_warned:\n            self._assembler.reset()\n"),
    ("repeat_warning", "pipeline.py", 4, "warnings",
     "if not self._mic_silent_warned:", "if True:"),
    ("warn_only_once_per_lesson", "pipeline.py", 4, "warnings",
     "if silent_for < self._silence_warning_s:\n            self._mic_silent_warned = False",
     "if silent_for < self._silence_warning_s:"),
    ("ignore_returning_voice", "pipeline.py", 4, "speaking_en",
     "await self._broadcast_speaking(self._speaking_signal.on_vad_activity())",
     "if not self._mic_silent_warned:\n                    await self._broadcast_speaking(self._speaking_signal.on_vad_activity())"),
    ("omit_following_chinese", "caption_delivery.py", 4, "captions_zh",
     "if self.roster.send(client, captions[selected]):",
     'if (selected != "zh" or job.utterance.seq != 2) and self.roster.send(client, captions[selected]):'),
)


def probe(root, cut):
    child = subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", str(root / "scripts/formal/live_audio_gap.py"),
         "--probe", "--case", str(cut), "340", "340"], cwd=root, encoding="utf-8", capture_output=True, timeout=30,
    )
    if child.returncode not in (0, 1):
        raise RuntimeError(f"scratch checker exited {child.returncode}: {child.stdout} {child.stderr}")
    report = json.loads(child.stdout)
    if report["status"] != ("PASS" if child.returncode == 0 else "FAIL"):
        raise RuntimeError(f"scratch status disagrees with its exit code: {report}")
    return report


def check_mutations():
    results = {}
    with tempfile.TemporaryDirectory(prefix="live-audio-gap-") as temp:
        scratch = Path(temp)
        shutil.copytree(ROOT / "server", scratch / "server", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "config.yaml", scratch)
        scripts = scratch / "scripts/formal"
        scripts.mkdir(parents=True)
        for name in ("live_audio_gap", "lesson_audio_boundary", "lesson_endurance", "lesson_liveness",
                     "morph_silence", "silero_contract"):
            shutil.copy2(ROOT / "scripts/formal" / f"{name}.py", scripts)
        controls = {cut: probe(scratch, cut) for cut in (4, 16)}
        for name, filename, cut, property_name, before, after in MUTATIONS:
            if controls[cut]["status"] != "PASS":
                results[name] = dict(detected=False, reason="unmodified witness already fails")
                continue
            target = scratch / "server" / filename
            original = target.read_bytes()
            source = target.read_text(encoding="utf-8")
            if source.count(before) != 1:
                raise ValueError(f"review {name} mutation in {filename}")
            try:
                target.write_text(source.replace(before, after), encoding="utf-8")
                broken = probe(scratch, cut)
            finally:
                target.write_bytes(original)
            witness = broken.get("control_failure") or broken.get("first_counterexamples", {}).get(property_name)
            if broken["status"] != "FAIL" or not witness or witness["property"] != property_name:
                raise RuntimeError(f"{name} missed {property_name}: {broken}")
            results[name] = dict(detected=True, **witness)
        if any(probe(scratch, cut) != control for cut, control in controls.items()):
            raise RuntimeError("restored scratch results differ from their controls")
    return results
