"""WebSocket境界の機能テスト（イシュー#10 受け入れ基準）。

インプロセスでサーバーを起動し、テストが先生クライアントと生徒クライアントを
演じる。ASR/翻訳は決定的フェイク（conftest で注入）。
以後のスライス（#11〜）もこの様式でテストを書く。
"""

from __future__ import annotations

import time

from server.ws_protocol import AsrFinal, Caption, Joined, JoinRejected, SessionStateMsg
from tests.conftest import JOIN_CODE
from tests.helpers import utterance_bytes, wait_for

PHRASE_1000 = "おはようございます。"
PHRASE_2000 = "光合成には日光が必要です。"
PHRASE_3000 = "今日は天気がいいですね。"


def join_student(
    ws, lang: str, code: str = JOIN_CODE, last_seq: int | None = None,
    *, expected: type[Joined] | type[JoinRejected] = Joined,
) -> dict:
    payload: dict = {"type": "join", "role": "student", "code": code, "lang": lang}
    if last_seq is not None:
        payload["last_seq"] = last_seq
    ws.send_json(payload)
    return wait_for(ws, expected).model_dump()


def join_teacher(ws, code: str = JOIN_CODE) -> dict:
    ws.send_json({"type": "join", "role": "teacher", "code": code})
    return wait_for(ws, Joined).model_dump()


def start_session(teacher) -> None:
    teacher.send_json({"type": "control", "action": "start"})
    wait_for(teacher, SessionStateMsg, state="live")


def send_utterance(teacher, key: int) -> None:
    for chunk in utterance_bytes(key):
        teacher.send_bytes(chunk)


class TestJoin:
    def test_student_join_ok(self, client):
        with client.websocket_connect("/ws") as student:
            msg = join_student(student, "en")
            assert msg["type"] == "joined"
            assert msg["seq_head"] == 0
            assert msg["history_from"] == 1  # 履歴なし＝次に発行されるseq
            assert msg["session_state"] == "idle"
            assert [lang["code"] for lang in msg["languages"]] == ["en", "zh"]



class TestCaptionFlow:
    def test_teacher_speech_reaches_student_in_selected_lang(self, client):
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            assert join_student(student, "en")["type"] == "joined"
            assert join_teacher(teacher)["type"] == "joined"
            start_session(teacher)
            wait_for(student, SessionStateMsg, state="live")

            send_utterance(teacher, key=2000)

            cap = wait_for(student, Caption)
            assert cap.seq == 1
            assert cap.lang == "en"
            assert cap.ja == PHRASE_2000
            assert cap.text == f"[en] {PHRASE_2000}"
            assert cap.delay_ms >= 0

            # 先生には日本語の確定結果が届く（ライブ文字起こし用）
            final = wait_for(teacher, AsrFinal)
            assert final.seq == 1
            assert final.ja == PHRASE_2000



class TestHttpEndpoints:
    def test_api_config_exposes_languages(self, client):
        res = client.get("/api/config")
        assert res.status_code == 200
        body = res.json()
        assert [lang["code"] for lang in body["languages"]] == ["en", "zh"]

    def test_student_page_served_on_root(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]

    def test_teacher_page_served(self, client):
        res = client.get("/teacher")
        assert res.status_code == 200

    def test_ready_ok(self, client):
        # warmup は背後で走るため、ロード完了(200)までポーリングする
        for _ in range(200):
            if client.get("/ready").status_code == 200:
                break
            time.sleep(0.02)
        assert client.get("/ready").status_code == 200

    def test_teacher_info_returns_join_code(self, client):
        res = client.get("/api/teacher-info")
        assert res.status_code == 200
        body = res.json()
        assert body["code"] == JOIN_CODE
        assert "join_url" in body
