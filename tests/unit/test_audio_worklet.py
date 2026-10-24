import shutil
import subprocess
from pathlib import Path

import pytest


def test_audio_worklet_resampling():
    node = shutil.which("node")
    if node is None:
        pytest.skip("AudioWorklet の変換処理検証には Node.js が必要")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, "--test", "tests/audio_worklet.cjs"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
