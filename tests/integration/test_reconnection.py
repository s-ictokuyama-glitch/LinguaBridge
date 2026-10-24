"""WS切断から授業への通知を通す疎通確認。"""
from server.ws_protocol import Caption, SessionStateMsg
from tests.helpers import wait_for
from tests.integration.test_ws_boundary import (
    PHRASE_1000, join_student, join_teacher, send_utterance, start_session,
)


class TestTeacherDisconnect:
    def test_disconnect_pauses_and_rejoin_resumes(self, client):
        with client.websocket_connect("/ws") as student:
            join_student(student, "en")
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                start_session(teacher)
                wait_for(student, SessionStateMsg, state="live")
            # 先生切断 → 自動一時停止バナー（E-07）
            wait_for(student, SessionStateMsg, state="paused")

            with client.websocket_connect("/ws") as teacher2:
                assert join_teacher(teacher2).type == "joined"
                # 再接続で自動再開
                wait_for(student, SessionStateMsg, state="live")
                send_utterance(teacher2, key=1000)
                assert wait_for(student, Caption).ja == PHRASE_1000
