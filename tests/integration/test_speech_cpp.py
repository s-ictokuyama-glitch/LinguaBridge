from __future__ import annotations

import os
import wave
from pathlib import Path

import numpy as np
import pytest

from scripts.speech_cpp import SpeechCppEngine

FIXTURE_WAV = Path(__file__).resolve().parents[1] / "fixtures" / "ja" / "03.wav"


@pytest.mark.timeout(240)
@pytest.mark.parametrize("quantization", ["Q8_0", "Q4_K"])
def test_speech_cpp_worker_transcribes_japanese(quantization: str) -> None:
    binary = os.environ.get("LINGUABRIDGE_SPEECH_CPP_BINARY")
    model_dir = os.environ.get("LINGUABRIDGE_SPEECH_CPP_MODEL_DIR")
    if not binary or not Path(binary).is_file():
        pytest.skip("LINGUABRIDGE_SPEECH_CPP_BINARY に speech.cpp の実行ファイルが必要")
    if not model_dir:
        pytest.skip("LINGUABRIDGE_SPEECH_CPP_MODEL_DIR に評価用 GGUF の場所が必要")
    model = Path(model_dir) / f"Haas-616M-ja-v1.0-{quantization}.gguf"
    if not model.is_file():
        pytest.skip(f"評価用 GGUF が未取得: {model.name}")
    if not FIXTURE_WAV.is_file():
        pytest.skip("fixture 未生成（scripts/make_fixture_audio.ps1 を実行）")

    with wave.open(str(FIXTURE_WAV), "rb") as audio:
        sample_rate = audio.getframerate()
        pcm16 = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
    with SpeechCppEngine(Path(binary), model, num_threads=0) as engine:
        engine.warmup()
        first = engine.transcribe(pcm16, sample_rate)
        second = engine.transcribe(pcm16, sample_rate)

    assert "教科書" in first.text
    assert "42" in first.text or "四十二" in first.text
    assert second.text == first.text

    with SpeechCppEngine(Path(binary), model, num_threads=0, keywords="教科図書=教科書") as engine:
        assert "教科図書" in engine.transcribe(pcm16, sample_rate).text
