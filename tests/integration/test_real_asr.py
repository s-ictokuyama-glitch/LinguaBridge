"""実モデルを使うASR統合テスト（イシュー#11）。

モデル（scripts/download_models.py）とフィクスチャ音声
（scripts/make_fixture_audio.ps1）が無い環境ではスキップされる。
"""

from __future__ import annotations

import time
import wave
from pathlib import Path

import numpy as np
import pytest
from starlette.testclient import TestClient

from server.asr.filter import hallucination_reason
from server.audio.vad import SileroVAD, VoiceSegmenter
from server.config import AppConfig, VadConfig
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE
from tests.helpers import SAMPLE_RATE, chunks, silence_pcm

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_WAV = ROOT / "tests" / "fixtures" / "ja" / "03.wav"  # 「教科書の四十二ページを開いてください。」
SMALL_MODEL_DIR = AppConfig().models.resolved_dir / "faster-whisper-small"

requires_models = pytest.mark.skipif(
    not SMALL_MODEL_DIR.exists(), reason="モデル未取得（scripts/download_models.py を実行）"
)
requires_fixture = pytest.mark.skipif(
    not FIXTURE_WAV.exists(), reason="fixture未生成（scripts/make_fixture_audio.ps1 を実行）"
)


def load_fixture_pcm() -> np.ndarray:
    with wave.open(str(FIXTURE_WAV), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def noise_pcm(seconds: float) -> np.ndarray:
    rng = np.random.default_rng(0)
    return rng.normal(0, 500, int(SAMPLE_RATE * seconds)).astype(np.int16)


@pytest.fixture(scope="module")
def fw_engine():
    from server.asr.fw_engine import FasterWhisperEngine

    engine = FasterWhisperEngine(SMALL_MODEL_DIR)
    engine.warmup()
    return engine


class TestSileroSegmentation:
    @requires_fixture
    def test_speech_fixture_yields_segments(self):
        seg = VoiceSegmenter(SileroVAD(0.5), min_silence_ms=500, max_utterance_s=30, frame_ms=32)
        pcm = np.concatenate([load_fixture_pcm(), silence_pcm(0.8)])
        segments = []
        for chunk in chunks(pcm):
            segments.extend(seg.feed(np.frombuffer(chunk, dtype=np.int16)))
        flushed = seg.flush()
        total = segments + ([flushed] if flushed else [])
        assert len(total) >= 1
        assert sum(s.pcm.size for s in total) > SAMPLE_RATE  # 1秒以上の音声が拾えている

    def test_noise_and_silence_yield_nothing(self):
        seg = VoiceSegmenter(SileroVAD(0.5), min_silence_ms=500, max_utterance_s=30, frame_ms=32)
        pcm = np.concatenate([noise_pcm(2.0), silence_pcm(2.0)])
        segments = []
        for chunk in chunks(pcm):
            segments.extend(seg.feed(np.frombuffer(chunk, dtype=np.int16)))
        assert segments == []
        assert seg.flush() is None


@requires_models
class TestFasterWhisperEngine:
    @requires_fixture
    def test_transcribes_japanese_fixture(self, fw_engine):
        result = fw_engine.transcribe(load_fixture_pcm(), SAMPLE_RATE)
        assert "教科書" in result.text
        assert "42" in result.text or "四十二" in result.text
        assert hallucination_reason(result) is None

    def test_silence_produces_no_deliverable_text(self, fw_engine):
        result = fw_engine.transcribe(silence_pcm(2.0), SAMPLE_RATE)
        assert hallucination_reason(result) is not None

    def test_noise_produces_no_deliverable_text(self, fw_engine):
        result = fw_engine.transcribe(noise_pcm(2.0), SAMPLE_RATE)
        assert hallucination_reason(result) is not None

    @requires_fixture
    def test_warmup_removes_first_call_spike(self, fw_engine):
        # module fixture で warmup 済み。初回呼び出しが定常時と同オーダーであること
        pcm = load_fixture_pcm()
        t0 = time.perf_counter()
        fw_engine.transcribe(pcm, SAMPLE_RATE)
        first = time.perf_counter() - t0
        t0 = time.perf_counter()
        fw_engine.transcribe(pcm, SAMPLE_RATE)
        steady = time.perf_counter() - t0
        assert first < steady * 3 + 0.5, f"first={first:.2f}s steady={steady:.2f}s"

    def test_rejects_wrong_sample_rate(self, fw_engine):
        with pytest.raises(ValueError):
            fw_engine.transcribe(silence_pcm(0.5), 48000)


@requires_models
@requires_fixture
class TestRealAsrOverWebSocket:
    def test_fixture_speech_reaches_student_as_caption(self):
        """受け入れ基準: 実発話（録音リプレイ）が文字起こしされ生徒カードに原文として届く。"""
        config = AppConfig(vad=VadConfig(engine="energy", threshold=300))
        app = create_app(
            config,
            mt_engine=FakeTranslationEngine(["en", "zh"]),
            join_code=JOIN_CODE,
        )
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as teacher,
                client.websocket_connect("/ws") as student,
            ):
                student.send_json(
                    {"type": "join", "role": "student", "code": JOIN_CODE, "lang": "en"}
                )
                assert student.receive_json()["type"] == "joined"
                teacher.send_json({"type": "join", "role": "teacher", "code": JOIN_CODE})
                assert teacher.receive_json()["type"] == "joined"
                teacher.send_json({"type": "control", "action": "start"})
                assert student.receive_json() == {"type": "session", "state": "live"}

                pcm = np.concatenate([load_fixture_pcm(), silence_pcm(0.8)])
                for chunk in chunks(pcm):
                    teacher.send_bytes(chunk)

                cap = student.receive_json()
                assert cap["type"] == "caption"
                assert "教科書" in cap["ja"]
                assert cap["text"] == f"[en] {cap['ja']}"  # フェイク訳文の原文=実文字起こし
