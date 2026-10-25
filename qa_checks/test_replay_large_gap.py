"""Shipped defaults and explicitly reduced replay-watermark boundaries."""

import pytest
from starlette.testclient import TestClient

from server.asr.fake_engine import FakeASREngine
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.integration.test_ws_boundary import join_student, join_teacher, send_utterance
from qa_checks.test_story_edges import receive_kind


@pytest.mark.parametrize("watermark,missing", [(128, 50), (32, 31), (32, 32), (32, 33)])
def test_conn04_cached_history_recovers_at_configured_watermark(watermark, missing):
    config = make_ws_test_config()
    assert config.history_resend == 50
    assert config.limits.mt_replay_watermark == 128
    config.limits.mt_replay_watermark = watermark
    app = create_app(config, asr_engine=FakeASREngine(), mt_engine=FakeTranslationEngine(), join_code=JOIN_CODE)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as observer:
            join_teacher(teacher)
            join_student(observer, "en")
            teacher.send_json({"type": "control", "action": "start"})
            receive_kind(observer, "session", state="live")
            for seq in range(1, missing + 1):
                send_utterance(teacher, key=1000)
                assert receive_kind(observer, "caption")["seq"] == seq
            # Every historical English translation already exists. Joining a
            # second English student must not require any new inference.
            with client.websocket_connect("/ws") as target:
                joined = join_student(target, "en", last_seq=0)
                assert joined["history_from"] == 1
                assert joined["seq_head"] == missing
                restored = [receive_kind(target, "caption")["seq"] for _ in range(missing)]
                assert restored == list(range(1, missing + 1))
