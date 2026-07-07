"""再接続・堅牢化（イシュー#13）のWS境界テスト。

切断・再接続をテストクライアントで再現し、差分復元・自動一時停止/再開・
後勝ち接続・総当たり対策を検証する。
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.asr.fake_engine import FakeASREngine
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from server.rate_limit import JoinRateLimiter
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.integration.test_ws_boundary import (
    PHRASE_1000,
    PHRASE_2000,
    PHRASE_3000,
    join_student,
    join_teacher,
    send_utterance,
    start_session,
)
from tests.unit.test_rate_limit import FakeClock


class TestStudentReconnect:
    def test_missed_captions_restored_without_gap(self, client):
        with client.websocket_connect("/ws") as teacher:
            join_teacher(teacher)
            start_session(teacher)

            with client.websocket_connect("/ws") as student:
                # start後の参加なので session_state は joined に載って届く
                assert join_student(student, "en")["session_state"] == "live"
                send_utterance(teacher, key=1000)
                assert student.receive_json()["seq"] == 1
            # ここで生徒は切断。切断中に2発話が配信される
            send_utterance(teacher, key=2000)
            send_utterance(teacher, key=3000)
            # 先生の asr_final を消費して2発話の処理完了を同期
            heads = [teacher.receive_json()["seq"] for _ in range(3)]
            assert heads == [1, 2, 3]

            with client.websocket_connect("/ws") as rejoined:
                msg = join_student(rejoined, "en", last_seq=1)
                assert msg["type"] == "joined"
                assert msg["seq_head"] == 3
                assert msg["history_from"] == 1
                replay = [rejoined.receive_json() for _ in range(2)]
                # 切断中に翻訳されていなかった分もオンデマンド翻訳で復元される
                assert [(c["seq"], c["ja"], c["text"]) for c in replay] == [
                    (2, PHRASE_2000, f"[en] {PHRASE_2000}"),
                    (3, PHRASE_3000, f"[en] {PHRASE_3000}"),
                ]
                # 復元後はライブ配信が継続する
                send_utterance(teacher, key=1000)
                live = rejoined.receive_json()
                assert live["seq"] == 4 and live["ja"] == PHRASE_1000

    def test_mixed_cached_and_ondemand_replay_keeps_order(self, client):
        """翻訳キャッシュ済みと未翻訳が混在する履歴の復元が seq 順で届く。"""
        with client.websocket_connect("/ws") as teacher:
            join_teacher(teacher)
            start_session(teacher)
            with client.websocket_connect("/ws") as observer:  # seq1-2の間だけ在席するen生徒
                join_student(observer, "en")
                with client.websocket_connect("/ws") as target:
                    join_student(target, "en")
                    send_utterance(teacher, key=1000)
                    assert target.receive_json()["seq"] == 1
                    assert observer.receive_json()["seq"] == 1
                # target切断中の seq2 は observer 在席のため翻訳キャッシュあり
                send_utterance(teacher, key=2000)
                assert observer.receive_json()["seq"] == 2
            # 全員切断後の seq3 は未翻訳のまま履歴に残る
            send_utterance(teacher, key=3000)
            assert [teacher.receive_json()["seq"] for _ in range(3)] == [1, 2, 3]

            with client.websocket_connect("/ws") as rejoined:
                assert join_student(rejoined, "en", last_seq=1)["type"] == "joined"
                replay = [rejoined.receive_json() for _ in range(2)]
                assert [(c["seq"], c["text"]) for c in replay] == [
                    (2, f"[en] {PHRASE_2000}"),
                    (3, f"[en] {PHRASE_3000}"),
                ]

    def test_long_disconnect_restores_up_to_history_limit(self, asr_engine, mt_engine):
        # 履歴上限3の構成で5発話 → last_seq=0 で再接続 → 直近3件だけ復元される
        config = make_ws_test_config()
        config.history_resend = 3
        app = create_app(
            config, asr_engine=asr_engine, mt_engine=mt_engine, join_code=JOIN_CODE
        )
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                start_session(teacher)
                for _ in range(5):
                    send_utterance(teacher, key=1000)
                assert [teacher.receive_json()["seq"] for _ in range(5)] == [1, 2, 3, 4, 5]

                with client.websocket_connect("/ws") as student:
                    msg = join_student(student, "en", last_seq=0)
                    assert msg["seq_head"] == 5
                    replay = [student.receive_json() for _ in range(3)]
                    assert [c["seq"] for c in replay] == [3, 4, 5]
                    # それ以上の再送はない: 次に届くのは新しい発話
                    send_utterance(teacher, key=2000)
                    assert student.receive_json()["seq"] == 6


class TestTeacherDisconnect:
    def test_disconnect_pauses_and_rejoin_resumes(self, client):
        with client.websocket_connect("/ws") as student:
            join_student(student, "en")
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                start_session(teacher)
                assert student.receive_json() == {"type": "session", "state": "live"}
            # 先生切断 → 自動一時停止バナー（E-07）
            assert student.receive_json() == {"type": "session", "state": "paused"}

            with client.websocket_connect("/ws") as teacher2:
                assert join_teacher(teacher2)["type"] == "joined"
                # 再接続で自動再開
                assert student.receive_json() == {"type": "session", "state": "live"}
                send_utterance(teacher2, key=1000)
                assert student.receive_json()["ja"] == PHRASE_1000

    def test_manual_pause_is_not_resumed_by_rejoin(self, client):
        with client.websocket_connect("/ws") as student:
            join_student(student, "en")
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                start_session(teacher)
                assert student.receive_json()["state"] == "live"
                teacher.send_json({"type": "control", "action": "pause"})
                assert student.receive_json()["state"] == "paused"
            # 手動一時停止のまま切断 → 再接続しても自動再開しない

            with client.websocket_connect("/ws") as teacher2:
                assert join_teacher(teacher2)["type"] == "joined"
                # 一時停止中の音声は破棄される（自動再開していない証拠）
                send_utterance(teacher2, key=1000)
                teacher2.send_json({"type": "control", "action": "start"})
                assert student.receive_json() == {"type": "session", "state": "live"}
                send_utterance(teacher2, key=2000)
                cap = student.receive_json()
                assert cap["ja"] == PHRASE_2000
                assert cap["seq"] == 1  # 一時停止中の発話は処理されていない

    def test_duplicate_teacher_last_wins_and_session_continues(self, client):
        with client.websocket_connect("/ws") as student:
            join_student(student, "en")
            with client.websocket_connect("/ws") as teacher1:
                join_teacher(teacher1)
                start_session(teacher1)
                assert student.receive_json()["state"] == "live"

                with client.websocket_connect("/ws") as teacher2:
                    assert join_teacher(teacher2)["type"] == "joined"
                    # 旧接続はサーバーから切断される（後勝ち E-08）
                    with pytest.raises(WebSocketDisconnect):
                        teacher1.receive_json()
                    # 後勝ちによる旧接続の切断で自動一時停止は起きず、新接続で配信継続
                    send_utterance(teacher2, key=1000)
                    assert student.receive_json()["ja"] == PHRASE_1000


class TestJoinRateLimit:
    def test_five_failures_block_then_unblock_after_60s(self, asr_engine, mt_engine):
        clock = FakeClock()
        app = create_app(
            make_ws_test_config(),
            asr_engine=asr_engine,
            mt_engine=mt_engine,
            join_code=JOIN_CODE,
            join_limiter=JoinRateLimiter(clock=clock),
        )
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                for _ in range(5):
                    assert join_student(ws, "en", code="0000")["reason"] == "bad_code"
                # ブロック中は正しいコードでも拒否される
                assert join_student(ws, "en")["reason"] == "rate_limited"
                # 60秒経過で解除される
                clock.now = 60.0
                assert join_student(ws, "en")["type"] == "joined"

    def test_success_resets_failure_count(self, asr_engine, mt_engine):
        clock = FakeClock()
        app = create_app(
            make_ws_test_config(),
            asr_engine=asr_engine,
            mt_engine=mt_engine,
            join_code=JOIN_CODE,
            join_limiter=JoinRateLimiter(clock=clock),
        )
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                for _ in range(4):
                    join_student(ws, "en", code="0000")
                assert join_student(ws, "en")["type"] == "joined"  # 成功でリセット
                for _ in range(4):
                    join_student(ws, "en", code="0000")
                assert join_student(ws, "en")["type"] == "joined"  # まだブロックされない