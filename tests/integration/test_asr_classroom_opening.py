"""Optional accuracy profile against the recorded classroom opening."""

import os
from pathlib import Path
import wave

import numpy as np
import pytest

from server.asr.hallucination import hallucination_reason
from server.audio.vad import Segment, VoiceSegmenter, build_frame_vad
from server.config import load_config
from server.main import build_asr_engine

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/ja/01.wav"


@pytest.mark.timeout(180)
def test_accuracy_profile_preserves_classroom_opening(record_property):
    config = load_config(ROOT / "config.yaml", override=ROOT / "config.accuracy.yaml")
    if models_dir := os.environ.get("LINGUABRIDGE_TEST_MODELS_DIR"):
        config.models.dir = models_dir
    model = config.asr.sherpa.model if config.asr.engine == "sherpa" else config.asr.model
    if not config.models.resolve(model).is_dir():
        pytest.skip("ASR model unavailable; run scripts/download_models.py")
    if not FIXTURE.is_file():
        pytest.skip("Audio fixture unavailable; run scripts/make_fixture_audio.ps1")
    with wave.open(str(FIXTURE), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
        pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)

    result = build_asr_engine(config).transcribe(pcm, 16000)
    record_property("asr_text", result.text)

    assert result.text.rstrip("。") == "今日は植物の光合成について勉強します"
    assert hallucination_reason(result) is None


@pytest.mark.timeout(180)
def test_accuracy_profile_preserves_browser_audio_after_vad(record_property):
    capture = os.environ.get("LINGUABRIDGE_TEST_BROWSER_AUDIO")
    if not capture or not Path(capture).is_file():
        pytest.skip("Set LINGUABRIDGE_TEST_BROWSER_AUDIO to the recorded teacher-sent.wav")
    config = load_config(ROOT / "config.yaml", override=ROOT / "config.accuracy.yaml")
    if models_dir := os.environ.get("LINGUABRIDGE_TEST_MODELS_DIR"):
        config.models.dir = models_dir
    if not config.models.resolve(config.asr.model).is_dir():
        pytest.skip("ASR model unavailable; run scripts/download_models.py")
    with wave.open(capture, "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
        pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)
    vad = build_frame_vad(config.vad)
    segmenter = VoiceSegmenter(
        vad, frame_ms=vad.frame_ms, pre_roll_ms=config.vad.pre_roll_ms,
        max_utterance_s=config.vad.max_utterance_s, **config.turn.segmenter_kwargs(config.vad),
    )
    replay = np.concatenate([pcm, np.zeros(32000, dtype=np.int16)])
    segments = []
    for start in range(0, len(replay), 1600):
        segments.extend(event for event in segmenter.feed(replay[start:start + 1600]) if isinstance(event, Segment))
    assert segments
    result = build_asr_engine(config).transcribe(segments[0].pcm, 16000)
    record_property("asr_text", result.text)
    record_property("segment_seconds", len(segments[0].pcm) / 16000)
    assert result.text.rstrip("。") == "今日は植物の光合成について勉強します"
    assert hallucination_reason(result) is None
