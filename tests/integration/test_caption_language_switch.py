from threading import Event

from tests.helpers import TestClient

from server.asr.fake_engine import FakeASREngine
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from server.publication import Publication
from server.ws_protocol import Caption, ErrorMsg, RecordingState
from tests.helpers import JOIN_CODE, make_ws_test_config, wait_for
from tests.integration.test_ws_boundary import (
    PHRASE_1000,
    PHRASE_2000,
    PHRASE_3000,
    join_student,
    join_teacher,
    send_utterance,
    start_session,
)


def test_language_switch_delivers_pending_caption_only_to_the_switching_student(tmp_path):
    release = Event()
    translating = Event()

    class GatedTranslation(FakeTranslationEngine):
        def translate(self, text_ja: str, target_lang: str) -> str:
            if text_ja == PHRASE_1000 and target_lang == "en":
                translating.set()
                assert release.wait(5)
            return super().translate(text_ja, target_lang)

    config = make_ws_test_config()
    config.recording.out_dir = str(tmp_path)
    config.recording.default_on = True
    app = create_app(
        config, Publication(config.server),
        asr_engine=FakeASREngine(),
        mt_engine=GatedTranslation(["en", "zh"]),
        join_code=JOIN_CODE,
    )

    def captions_until_recording_off(socket):
        received = []
        wait_for(socket, RecordingState, on=False, received=received)
        return [(msg.seq, msg.text) for msg in received if isinstance(msg, Caption)]

    with (
        TestClient(app) as client,
        client.websocket_connect("/ws") as teacher,
        client.websocket_connect("/ws") as english,
    ):
        join_teacher(teacher)
        join_student(english, "en")
        start_session(teacher)
        with client.websocket_connect("/ws") as switching:
            join_student(switching, "en")
            send_utterance(teacher, key=1000)
            try:
                assert translating.wait(3)
                switching.send_json({"type": "set_lang", "lang": "zh"})
                switching.send_json({"type": "set_lang", "lang": "unsupported"})
                wait_for(switching, ErrorMsg, code="bad_lang")
                with client.websocket_connect("/ws") as chinese:
                    join_student(chinese, "zh")
                    release.set()
                    send_utterance(teacher, key=2000)
                    teacher.send_json({"type": "control", "action": "end"})
                    teacher.send_json({"type": "recording", "on": False})

                    assert captions_until_recording_off(english) == [
                        (1, f"[en] {PHRASE_1000}"),
                        (2, f"[en] {PHRASE_2000}"),
                    ]
                    assert captions_until_recording_off(chinese) == [(2, f"[zh] {PHRASE_2000}")]
                    assert captions_until_recording_off(switching) == [
                        (1, f"[zh] {PHRASE_1000}"),
                        (2, f"[zh] {PHRASE_2000}"),
                    ]
            finally:
                release.set()

        with client.websocket_connect("/ws") as rejoined:
            join_student(rejoined, "zh", last_seq=2)
            start_session(teacher)
            send_utterance(teacher, key=3000)
            caption = wait_for(rejoined, Caption)
            assert (caption.seq, caption.text) == (3, f"[zh] {PHRASE_3000}")
