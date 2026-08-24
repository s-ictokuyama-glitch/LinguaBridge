"""turn 戦略が字幕カードの粒度を変えることのWS境界テスト（#27）。

同じ音声列を `simple` と `morph` に流し、**外から見える結果**（生徒に届く
caption の枚数と原文）が設計どおりに変わることを確認する。

音声列: 「今日は天気がいいですね。」(3000) → 600ms の間 → 「光合成には日光が必要です。」(2000)

`600ms` は simple の min_silence_ms(500) 以上なので現行は必ず2枚に割る。
morph では check_silence_ms(320) 以上・force_silence_ms(640) 未満なので、
**Segment は割れるが Turn は割ってはいけない**位置。前半は `〜ですね` で終わるため
継続クラスになり、後半の句点で1枚に確定するのが期待動作。
"""

from __future__ import annotations

import numpy as np
from starlette.testclient import TestClient

from server.asr.fake_engine import FakeASREngine
from server.config import TurnConfig
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.helpers import chunks, silence_pcm, speech_pcm
from tests.integration.test_ws_boundary import join_student, join_teacher, start_session

CONTINUING = FakeASREngine.PHRASES[3000]  # 「今日は天気がいいですね。」= 継続クラス
TERMINAL = FakeASREngine.PHRASES[2000]  # 「光合成には日光が必要です。」= StrongEnd


def make_app(strategy: str):
    config = make_ws_test_config()
    config.turn = TurnConfig(strategy=strategy)
    return create_app(
        config,
        asr_engine=FakeASREngine(),
        mt_engine=FakeTranslationEngine(["en"]),
        join_code=JOIN_CODE,
    )


def send_two_segments_with_a_short_gap(teacher) -> None:
    """simple は割り morph は割らない長さ（500ms 以上 640ms 未満）の間を挟んだ2発話。"""
    pcm = np.concatenate(
        [
            speech_pcm(3000, 0.6),
            silence_pcm(0.6),  # simple: 分割 / morph: Segment だけ分割
            speech_pcm(2000, 0.6),
            silence_pcm(1.2),
        ]
    )
    for chunk in chunks(pcm):
        teacher.send_bytes(chunk)


def captions(strategy: str) -> list[dict]:
    app = make_app(strategy)
    with TestClient(app) as client:
        with (
            client.websocket_connect("/ws") as teacher,
            client.websocket_connect("/ws") as student,
        ):
            join_student(student, "en")
            join_teacher(teacher)
            start_session(teacher)
            assert student.receive_json() == {"type": "session", "state": "live"}
            send_two_segments_with_a_short_gap(teacher)
            first = student.receive_json()
            assert first["type"] == "caption"
            teacher.send_json({"type": "control", "action": "end"})
            out = [first]
            for _ in range(4):  # 2枚目があれば拾う。無ければ session/ended で抜ける
                msg = student.receive_json()
                if msg["type"] != "caption":
                    break
                out.append(msg)
            return out


def test_simple_splits_at_every_silence():
    """既定の simple は #27 以前と同じく、無音長だけで2枚に割る。"""
    cards = captions("simple")
    assert [c["ja"] for c in cards] == [CONTINUING, TERMINAL]
    assert [c["seq"] for c in cards] == [1, 2]


def test_morph_merges_across_a_mid_sentence_gap():
    """morph は継続クラスの Segment を次と連結し、字幕カードを1枚にする。"""
    cards = captions("morph")
    assert len(cards) == 1
    assert cards[0]["ja"] == CONTINUING + TERMINAL
    assert cards[0]["seq"] == 1
    # 生徒に届く訳文も連結後の1件（＝ Hy-MT2 の呼び出しが1回で済む）
    assert cards[0]["text"] == f"[en] {CONTINUING + TERMINAL}"
