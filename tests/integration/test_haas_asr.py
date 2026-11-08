from __future__ import annotations

import os
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from server import ws_protocol as proto
from server.asr.haas_sherpa_engine import MODEL_FILES
from server.config import AppConfig, AsrConfig, HaasOnnxConfig, ModelsConfig, TurnConfig, VadConfig
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from server.publication import Publication
from tests.helpers import JOIN_CODE, TEACHER_CODE, TestClient, chunks, silence_pcm, wait_for

pytestmark = pytest.mark.timeout(180)
FIXTURE_WAV = Path(__file__).resolve().parents[1] / "fixtures" / "ja" / "03.wav"


@pytest.mark.skipif(not FIXTURE_WAV.is_file(), reason="fixture未生成（scripts/make_fixture_audio.ps1）")
@pytest.mark.parametrize("engine,quantization", [("haas-sherpa", "int4"), ("haas-onnx", "int4"), ("haas-onnx", "int8")])
def test_haas_config_warms_up_and_delivers_japanese_caption(engine, quantization):
    models = ModelsConfig(dir=os.environ.get("LINGUABRIDGE_HAAS_MODELS", ModelsConfig().dir))
    asr = AsrConfig(engine=engine, haas_onnx=HaasOnnxConfig(quantization=quantization))
    if engine == "haas-sherpa":
        model_dir = models.resolve(asr.haas_sherpa.model)
        required = MODEL_FILES
    else:
        model_dir = models.resolve(asr.haas_onnx.model)
        fmt = "int4-onecomp" if quantization == "int4" else "int8"
        required = (
            "manifest.json", f"{fmt}/encoder.onnx", f"{fmt}/encoder.onnx.data",
            "common/frontend.json", "common/tokens.json", "common/decoding.json",
            "common/prediction.onnx", "common/joint.onnx",
        )
    if any(not (model_dir / name).is_file() for name in required):
        pytest.skip(f"Haas-ja モデル未取得: {model_dir}")

    config = AppConfig(
        models=models, asr=asr, turn=TurnConfig(strategy="simple"),
        vad=VadConfig(engine="energy", threshold=300),
    )
    app = create_app(
        config, Publication(config.server),
        mt_engine=FakeTranslationEngine(config.language_codes),
        join_code=JOIN_CODE, teacher_code=TEACHER_CODE,
    )
    with wave.open(str(FIXTURE_WAV), "rb") as fixture:
        pcm = np.frombuffer(fixture.readframes(fixture.getnframes()), dtype=np.int16)
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws") as teacher,
        client.websocket_connect("/ws") as student,
    ):
        deadline = time.monotonic() + 120
        while client.get("/ready").status_code != 200:
            assert time.monotonic() < deadline, "Haas-ja の warmup が完了しませんでした"
            time.sleep(0.1)
        student.send_json({"type": "join", "role": "student", "code": JOIN_CODE, "lang": "en"})
        wait_for(student, proto.Joined)
        teacher.send_json({"type": "join", "role": "teacher", "code": TEACHER_CODE})
        wait_for(teacher, proto.Joined)
        teacher.send_json({"type": "control", "action": "start"})
        wait_for(student, proto.SessionStateMsg, state="live")
        for chunk in chunks(np.concatenate([pcm, silence_pcm(1.2)])):
            teacher.send_bytes(chunk)

        caption = wait_for(student, proto.Caption)
        assert "教科書" in caption.ja
        assert "42ページ" in caption.ja or "四十二ページ" in caption.ja
        assert "開いてください" in caption.ja
