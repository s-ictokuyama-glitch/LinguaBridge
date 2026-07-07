"""WebSocket境界の機能テスト（イシュー#10 受け入れ基準）。

インプロセスでサーバーを起動し、テストが先生クライアントと生徒クライアントを
演じる。ASR/翻訳は決定的フェイク（conftest で注入）。
以後のスライス（#11〜）もこの様式でテストを書く。
"""

from __future__ import annotations

from tests.conftest import JOIN_CODE
from tests.helpers import utterance_bytes

PHRASE_1000 = "おはようございます。"
PHRASE_2000 = "光合成には日光が必要です。"


def join_student(ws, lang: str, code: str = JOIN_CODE, last_seq: int | None = None) -> dict:
    payload: dict = {"type": "join", "role": "student", "code": code, "lang": lang}
    if last_seq is not None:
        payload["last_seq"] = last_seq
    ws.send_json(payload)
    return ws.receive_json()


def join_teacher(ws, code: str = JOIN_CODE) -> dict:
    ws.send_json({"type": "join", "role": "teacher", "code": code})
    return ws.receive_json()


def start_session(teacher) -> None:
    teacher.send_json({"type": "control", "action": "start"})
    msg = teacher.receive_json()
    assert msg == {"type": "session", "state": "live"}


def send_utterance(teacher, key: int) -> None:
    for chunk in utterance_bytes(key):
        teacher.send_bytes(chunk)


class TestJoin:
    def test_student_join_ok(self, client):
        with client.websocket_connect("/ws") as student:
            msg = join_student(student, "en")
            assert msg["type"] == "joined"
            assert msg["seq_head"] == 0
            assert msg["session_state"] == "idle"
            assert [lang["code"] for lang in msg["languages"]] == ["en", "zh"]

    def test_bad_code_rejected_then_retry_succeeds(self, client):
        with client.websocket_connect("/ws") as student:
            msg = join_student(student, "en", code="0000")
            assert msg == {"type": "join_rejected", "reason": "bad_code"}
            # 拒否後も接続は維持され、正しいコードで再参加できる
            msg = join_student(student, "en")
            assert msg["type"] == "joined"

    def test_unsupported_lang_rejected(self, client):
        with client.websocket_connect("/ws") as student:
            msg = join_student(student, "fr")
            assert msg == {"type": "join_rejected", "reason": "bad_lang"}

    def test_student_join_without_lang_rejected(self, client):
        with client.websocket_connect("/ws") as student:
            student.send_json({"type": "join", "role": "student", "code": JOIN_CODE})
            assert student.receive_json() == {"type": "join_rejected", "reason": "bad_lang"}

    def test_teacher_bad_code_rejected(self, client):
        with client.websocket_connect("/ws") as teacher:
            msg = join_teacher(teacher, code="9999")
            assert msg == {"type": "join_rejected", "reason": "bad_code"}


class TestCaptionFlow:
    def test_teacher_speech_reaches_student_in_selected_lang(self, client):
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            assert join_student(student, "en")["type"] == "joined"
            assert join_teacher(teacher)["type"] == "joined"
            start_session(teacher)
            assert student.receive_json() == {"type": "session", "state": "live"}

            send_utterance(teacher, key=2000)

            cap = student.receive_json()
            assert cap["type"] == "caption"
            assert cap["seq"] == 1
            assert cap["lang"] == "en"
            assert cap["ja"] == PHRASE_2000
            assert cap["text"] == f"[en] {PHRASE_2000}"
            assert cap["delay_ms"] >= 0

            # 先生には日本語の確定結果が届く（ライブ文字起こし用）
            final = teacher.receive_json()
            assert final["type"] == "asr_final"
            assert final["seq"] == 1
            assert final["ja"] == PHRASE_2000

    def test_students_receive_only_their_language(self, client, mt_engine):
        with (
            client.websocket_connect("/ws") as teacher,
            client.websocket_connect("/ws") as student_en,
            client.websocket_connect("/ws") as student_zh,
        ):
            join_student(student_en, "en")
            join_student(student_zh, "zh")
            join_teacher(teacher)
            start_session(teacher)
            student_en.receive_json()  # session live
            student_zh.receive_json()

            send_utterance(teacher, key=1000)

            cap_en = student_en.receive_json()
            cap_zh = student_zh.receive_json()
            assert cap_en["lang"] == "en" and cap_en["text"] == f"[en] {PHRASE_1000}"
            assert cap_zh["lang"] == "zh" and cap_zh["text"] == f"[zh] {PHRASE_1000}"

    def test_no_translation_job_for_inactive_language(self, client, mt_engine):
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(teacher)
            start_session(teacher)
            student.receive_json()  # session live

            # 2発話流す。MTワーカーはFIFOなので、2発話目のcaption受信時点で
            # 1発話目のジョブは全て処理済み＝zhジョブの不在を確定的に検査できる
            send_utterance(teacher, key=1000)
            send_utterance(teacher, key=2000)
            assert student.receive_json()["ja"] == PHRASE_1000
            assert student.receive_json()["ja"] == PHRASE_2000

            assert (PHRASE_1000, "en") in mt_engine.calls
            langs_called = {lang for _, lang in mt_engine.calls}
            assert langs_called == {"en"}, "選択者0名のzhに翻訳ジョブが発生している"

    def test_set_lang_switches_captions(self, client, mt_engine):
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(teacher)
            start_session(teacher)
            student.receive_json()  # session live

            send_utterance(teacher, key=1000)
            assert student.receive_json()["lang"] == "en"

            student.send_json({"type": "set_lang", "lang": "zh"})
            send_utterance(teacher, key=2000)
            cap = student.receive_json()
            assert cap["lang"] == "zh"
            assert cap["text"] == f"[zh] {PHRASE_2000}"
            # 切替後は en が非アクティブになり、2発話目に en ジョブは発生しない
            assert (PHRASE_2000, "en") not in mt_engine.calls


class TestSessionControl:
    def test_pause_and_end_broadcast_to_students(self, client):
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(teacher)
            start_session(teacher)
            assert student.receive_json() == {"type": "session", "state": "live"}

            teacher.send_json({"type": "control", "action": "pause"})
            assert student.receive_json() == {"type": "session", "state": "paused"}

            teacher.send_json({"type": "control", "action": "end"})
            assert student.receive_json() == {"type": "session", "state": "ended"}

    def test_audio_ignored_while_paused(self, client, asr_engine):
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(teacher)
            start_session(teacher)
            student.receive_json()  # live

            teacher.send_json({"type": "control", "action": "pause"})
            assert student.receive_json() == {"type": "session", "state": "paused"}
            send_utterance(teacher, key=1000)  # 一時停止中の音声は破棄される

            teacher.send_json({"type": "control", "action": "start"})
            assert student.receive_json() == {"type": "session", "state": "live"}
            send_utterance(teacher, key=2000)

            cap = student.receive_json()
            assert cap["ja"] == PHRASE_2000  # 復帰後の発話のみ届く
            transcribed = [r.text for r in asr_engine.calls]
            assert PHRASE_1000 not in transcribed


class TestHistoryResend:
    def test_reconnect_with_last_seq_receives_missed_captions(self, client):
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(teacher)
            start_session(teacher)
            student.receive_json()  # live
            send_utterance(teacher, key=1000)
            assert student.receive_json()["seq"] == 1

            # 再接続を演じる: last_seq=0 で新規接続 → seq=1 が再送される
            with client.websocket_connect("/ws") as rejoined:
                msg = join_student(rejoined, "en", last_seq=0)
                assert msg["type"] == "joined"
                assert msg["seq_head"] == 1
                replay = rejoined.receive_json()
                assert replay["type"] == "caption"
                assert replay["seq"] == 1
                assert replay["text"] == f"[en] {PHRASE_1000}"

    def test_reconnect_up_to_date_receives_nothing_extra(self, client):
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(teacher)
            start_session(teacher)
            student.receive_json()  # live
            send_utterance(teacher, key=1000)
            assert student.receive_json()["seq"] == 1

            with client.websocket_connect("/ws") as rejoined:
                msg = join_student(rejoined, "en", last_seq=1)
                assert msg["seq_head"] == 1
                # 追加の再送はない: 次に届くのは新しい発話のcaptionのみ
                send_utterance(teacher, key=2000)
                nxt = rejoined.receive_json()
                assert nxt["seq"] == 2


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

    def test_healthz_ok(self, client):
        assert client.get("/healthz").status_code == 200

    def test_teacher_info_returns_join_code(self, client):
        res = client.get("/api/teacher-info")
        assert res.status_code == 200
        body = res.json()
        assert body["code"] == JOIN_CODE
        assert "join_url" in body
