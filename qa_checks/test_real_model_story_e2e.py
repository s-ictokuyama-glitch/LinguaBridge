"""実モデルが必要な ASR-01 / ASR-06 のストーリー受け入れ試験。"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest
from starlette.testclient import TestClient

from server.config import AppConfig, AsrConfig, MtConfig, SherpaConfig, VadConfig
from server.main import build_asr_engine, create_app
from tests.conftest import JOIN_CODE
from tests.helpers import chunks, silence_pcm
from tests.integration.test_ws_boundary import join_student, join_teacher, start_session
from tests.net_guard import guard_network


ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = AppConfig().models.resolved_dir
REAZON_MODEL_DIR = MODELS_DIR / "reazonspeech-k2-v2"
HYMT_MODEL_FILE = MODELS_DIR / "hy-mt2" / "Hy-MT2-1.8B-Q4_K_M.gguf"
ASR_STORY_WAV = ROOT / "tests" / "fixtures" / "ja" / "03.wav"
HOTWORD_STORY_WAV = (
    ROOT / "tests" / "fixtures" / "ja_ext" / "noise-snr10-rate-fast-b.wav"
)


def _missing(*paths: Path) -> list[Path]:
    return [path for path in paths if not path.exists()]


def _load_pcm(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)


def _receive_caption(ws) -> dict:
    """実クライアント同様、speaking通知を越えて確定字幕を待つ。"""
    seen: list[str] = []
    for _ in range(10):
        message = ws.receive_json()
        seen.append(message.get("type", ""))
        if message.get("type") == "caption":
            return message
    raise AssertionError(f"caption が10メッセージ以内に届きませんでした: {seen}")


@pytest.mark.timeout(120)
def test_asr01_real_japanese_audio_reaches_selected_languages_locally() -> None:
    """実sherpaの原文が実Hy-MT2を経て、選択言語の字幕として届く。"""
    missing = _missing(REAZON_MODEL_DIR, HYMT_MODEL_FILE, ASR_STORY_WAV)
    if missing:
        pytest.skip(f"実モデルまたはWAVがありません: {missing}")

    config = AppConfig(
        asr=AsrConfig(engine="sherpa"),
        mt=MtConfig(engine="hy-mt2"),
        vad=VadConfig(engine="energy", threshold=300),
    )
    pcm = np.concatenate([_load_pcm(ASR_STORY_WAV), silence_pcm(0.8)])

    with guard_network() as network:
        app = create_app(config, join_code=JOIN_CODE)
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as teacher,
                client.websocket_connect("/ws") as student_en,
                client.websocket_connect("/ws") as student_zh,
            ):
                assert join_student(student_en, "en")["type"] == "joined"
                assert join_student(student_zh, "zh")["type"] == "joined"
                assert join_teacher(teacher)["type"] == "joined"
                start_session(teacher)
                assert student_en.receive_json() == {"type": "session", "state": "live"}
                assert student_zh.receive_json() == {"type": "session", "state": "live"}

                for chunk in chunks(pcm):
                    teacher.send_bytes(chunk)

                caption_en = _receive_caption(student_en)
                caption_zh = _receive_caption(student_zh)

    network.assert_no_external_traffic()
    assert network.external_lookups == []
    assert caption_en["seq"] == caption_zh["seq"] == 1
    assert caption_en["ja"] == caption_zh["ja"]
    assert "教科書" in caption_en["ja"]
    assert "四十二" in caption_en["ja"] or "42" in caption_en["ja"]
    assert caption_en["lang"] == "en"
    assert "textbook" in caption_en["text"].lower()
    assert "42" in caption_en["text"]
    assert caption_zh["lang"] == "zh"
    assert "教科书" in caption_zh["text"] or "教科書" in caption_zh["text"]
    assert "四十二" in caption_zh["text"] or "42" in caption_zh["text"]


@pytest.mark.timeout(120)
def test_asr06_configured_hotword_corrects_known_domain_term(tmp_path: Path) -> None:
    """config→factoryで渡したhotwordが、同一の実音声の認識結果を補正する。"""
    missing = _missing(REAZON_MODEL_DIR, HOTWORD_STORY_WAV)
    if missing:
        pytest.skip(f"実モデルまたはWAVがありません: {missing}")

    hotwords_file = tmp_path / "hotwords.txt"
    hotwords_file.write_text("原 子 の 数\n", encoding="utf-8")
    pcm = _load_pcm(HOTWORD_STORY_WAV)

    with guard_network() as network:
        without_hotword = build_asr_engine(
            AppConfig(
                asr=AsrConfig(
                    engine="sherpa",
                    sherpa=SherpaConfig(
                        decoding_method="modified_beam_search",
                        lead_in_ms=600,
                        hotwords_file=None,
                    ),
                )
            )
        )
        without_hotword.warmup()
        uncorrected = without_hotword.transcribe(pcm, 16000).text

        with_hotword = build_asr_engine(
            AppConfig(
                asr=AsrConfig(
                    engine="sherpa",
                    sherpa=SherpaConfig(
                        decoding_method="modified_beam_search",
                        lead_in_ms=600,
                        hotwords_file=str(hotwords_file),
                    ),
                )
            )
        )
        with_hotword.warmup()
        corrected = with_hotword.transcribe(pcm, 16000).text

    network.assert_no_external_traffic()
    assert network.external_lookups == []

    assert "原子の風" in uncorrected
    assert "原子の数" in corrected
    assert corrected != uncorrected
