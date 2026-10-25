"""OPS-06 の実モデル直積を、設定から字幕配信まで通して確認する。"""

from __future__ import annotations

import gc
from pathlib import Path
import wave

import numpy as np
import pytest
from starlette.testclient import TestClient

from server.config import AppConfig, AsrConfig, MtConfig, VadConfig
from server.main import build_asr_engine, build_mt_engine, create_app
from tests.conftest import JOIN_CODE
from tests.helpers import chunks, silence_pcm
from tests.net_guard import guard_network


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_WAV = ROOT / "tests" / "fixtures" / "ja" / "03.wav"
MODELS_DIR = AppConfig().models.resolved_dir
REQUIRED_MODEL_PATHS = (
    MODELS_DIR / "reazonspeech-k2-v2",
    MODELS_DIR / "faster-whisper-small",
    MODELS_DIR / "hy-mt2" / "Hy-MT2-1.8B-Q4_K_M.gguf",
    MODELS_DIR / "nllb-200-distilled-600M-ct2",
    MODELS_DIR / "nllb-tokenizer",
)


def _load_fixture_pcm() -> np.ndarray:
    with wave.open(str(FIXTURE_WAV), "rb") as wav:
        assert wav.getframerate() == 16_000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)


def _receive_caption(ws) -> dict[str, object]:  # type: ignore[no-untyped-def]
    """モデル処理中の統計フレーム等を読み飛ばし、最終字幕を返す。"""

    for _ in range(30):
        message = ws.receive_json()
        if message.get("type") == "caption":
            return message
    raise AssertionError("30フレーム以内にcaptionを受信できませんでした")


@pytest.mark.skipif(
    not FIXTURE_WAV.exists() or any(not path.exists() for path in REQUIRED_MODEL_PATHS),
    reason="4組合せに必要な実モデルまたは日本語音源がありません",
)
@pytest.mark.timeout(240)
def test_ops06_all_real_engine_combinations_deliver_en_and_zh() -> None:
    """sherpa/Faster-Whisper × Hy-MT2/NLLB を実際の授業経路で切り替える。"""

    expected_types = {
        "sherpa": "SherpaOnnxEngine",
        "faster-whisper": "FasterWhisperEngine",
        "hy-mt2": "HyMt2Engine",
        "nllb": "NllbEngine",
    }
    pcm = np.concatenate([_load_fixture_pcm(), silence_pcm(0.8)])
    observed: set[tuple[str, str]] = set()

    for asr_name in ("sherpa", "faster-whisper"):
        for mt_name in ("hy-mt2", "nllb"):
            config = AppConfig(
                asr=AsrConfig(engine=asr_name),
                mt=MtConfig(engine=mt_name),
                vad=VadConfig(engine="energy", threshold=300),
            )
            with guard_network() as network:
                asr_engine = build_asr_engine(config)
                mt_engine = build_mt_engine(config)
                assert type(asr_engine).__name__ == expected_types[asr_name]
                assert type(mt_engine).__name__ == expected_types[mt_name]

                app = create_app(
                    config,
                    asr_engine=asr_engine,
                    mt_engine=mt_engine,
                    join_code=JOIN_CODE,
                )
                with TestClient(app) as client:
                    with (
                        client.websocket_connect("/ws") as teacher,
                        client.websocket_connect("/ws") as english,
                        client.websocket_connect("/ws") as chinese,
                    ):
                        english.send_json(
                            {
                                "type": "join",
                                "role": "student",
                                "code": JOIN_CODE,
                                "lang": "en",
                            }
                        )
                        assert english.receive_json()["type"] == "joined"
                        chinese.send_json(
                            {
                                "type": "join",
                                "role": "student",
                                "code": JOIN_CODE,
                                "lang": "zh",
                            }
                        )
                        assert chinese.receive_json()["type"] == "joined"
                        teacher.send_json(
                            {"type": "join", "role": "teacher", "code": JOIN_CODE}
                        )
                        assert teacher.receive_json()["type"] == "joined"
                        teacher.send_json({"type": "control", "action": "start"})
                        assert english.receive_json() == {"type": "session", "state": "live"}
                        assert chinese.receive_json() == {"type": "session", "state": "live"}

                        for chunk in chunks(pcm):
                            teacher.send_bytes(chunk)

                        en_caption = _receive_caption(english)
                        zh_caption = _receive_caption(chinese)

                network.assert_no_external_traffic()
                assert network.external_lookups == []

            assert en_caption["lang"] == "en"
            assert zh_caption["lang"] == "zh"
            assert en_caption["seq"] == zh_caption["seq"] == 1
            assert en_caption["ja"] == zh_caption["ja"]
            assert "教科書" in str(en_caption["ja"])
            assert "42" in str(en_caption["ja"]) or "四十二" in str(en_caption["ja"])
            assert "textbook" in str(en_caption["text"]).lower()
            assert "42" in str(en_caption["text"])
            assert "教科书" in str(zh_caption["text"]) or "教科書" in str(
                zh_caption["text"]
            )
            assert "42" in str(zh_caption["text"]) or "四十二" in str(zh_caption["text"])
            observed.add((asr_name, mt_name))

            del app, asr_engine, mt_engine
            gc.collect()

    assert observed == {
        ("sherpa", "hy-mt2"),
        ("sherpa", "nllb"),
        ("faster-whisper", "hy-mt2"),
        ("faster-whisper", "nllb"),
    }
