from threading import Event

import pytest
from tests.helpers import TestClient
from starlette.websockets import WebSocketDisconnect

from server.asr.fake_engine import FakeASREngine
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from server.publication import Publication
from server.ws_protocol import Caption, ErrorMsg
from tests.helpers import JOIN_CODE, make_ws_test_config, wait_for
from tests.integration.test_ws_boundary import join_student, join_teacher, send_utterance, start_session


def make_app(mt, *, capacity=8, watermark=1):
    config = make_ws_test_config()
    config.limits.mt_queue_max = capacity
    config.limits.mt_replay_watermark = watermark
    return create_app(
        config, Publication(config.server), asr_engine=FakeASREngine(),
        mt_engine=mt, join_code=JOIN_CODE,
    )


def test_reconnect_can_retry_after_replay_admission_fails():
    app = make_app(FakeTranslationEngine())
    with TestClient(app) as client, client.websocket_connect("/ws") as teacher:
        join_teacher(teacher)
        start_session(teacher)
        with client.websocket_connect("/ws") as student:
            join_student(student, "en")
            for seq, key in enumerate((1000, 2000), 1):
                send_utterance(teacher, key)
                assert wait_for(student, Caption).seq == seq

        with client.websocket_connect("/ws") as student:
            join_student(student, "en", last_seq=0)
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    student.receive_text()
            assert closed.value.code == 1013

        with client.websocket_connect("/ws") as student:
            join_student(student, "en", last_seq=1)
            assert wait_for(student, Caption).seq == 2


def test_full_live_queue_delivers_every_confirmed_caption():
    started = Event()
    release = Event()

    class HeldTranslation(FakeTranslationEngine):
        def translate(self, text_ja, target_lang):
            started.set()
            assert release.wait(5)
            return super().translate(text_ja, target_lang)

    app = make_app(HeldTranslation(), capacity=1)
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws") as teacher,
        client.websocket_connect("/ws") as student,
    ):
        join_teacher(teacher)
        join_student(student, "en")
        start_session(teacher)
        try:
            send_utterance(teacher, 1000)
            assert started.wait(3)
            send_utterance(teacher, 2000)
            send_utterance(teacher, 3000)
            wait_for(teacher, ErrorMsg, code="mt_backlog")
        finally:
            release.set()
        assert [wait_for(student, Caption).seq for _ in range(3)] == [1, 2, 3]
