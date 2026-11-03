import json
from contextlib import contextmanager
from threading import Event

import pytest
from starlette.testclient import TestClient

from server.asr.fake_engine import FakeASREngine
from server.config import Language
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from server.publication import Publication
from server.ws_protocol import Caption, ErrorMsg, RecordingState, Stats
from tests.helpers import JOIN_CODE, make_ws_test_config, wait_for
from tests.integration.test_ws_boundary import (
    PHRASE_1000, PHRASE_2000, PHRASE_3000,
    join_student, join_teacher, send_utterance, start_session,
)


class HeldRefresh(FakeTranslationEngine):
    def __init__(self, held_lang="zh"):
        super().__init__(["en", "zh", "ko"])
        self.held_lang = held_lang
        self.started = Event()
        self.release = Event()

    def translate(self, text_ja: str, target_lang: str) -> str:
        if target_lang == self.held_lang and text_ja == PHRASE_1000:
            self.started.set()
            assert self.release.wait(5)
        return super().translate(text_ja, target_lang)


@contextmanager
def classroom(tmp_path, mt, *, watermark=128):
    config = make_ws_test_config()
    config.languages.append(Language(code="ko", label="한국어"))
    config.recording.out_dir = str(tmp_path)
    config.recording.default_on = True
    config.limits.mt_replay_watermark = watermark
    config.monitoring.stats_interval_s = 0.05
    app = create_app(
        config, Publication(config.server), asr_engine=FakeASREngine(),
        mt_engine=mt, join_code=JOIN_CODE,
    )
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws") as teacher,
        client.websocket_connect("/ws") as student,
        client.websocket_connect("/ws") as peer,
    ):
        join_teacher(teacher)
        join_student(student, "en")
        join_student(peer, "en")
        start_session(teacher)
        for seq, key in enumerate((1000, 2000), 1):
            send_utterance(teacher, key)
            for socket in (student, peer):
                assert wait_for(socket, Caption).seq == seq
        yield teacher, student, peer


def switch_language(student, lang):
    student.send_json({"type": "set_lang", "lang": lang})
    student.send_json({"type": "set_lang", "lang": "unsupported"})
    messages = []
    wait_for(student, ErrorMsg, code="bad_lang", received=messages)
    return [(msg.seq, msg.text) for msg in messages if isinstance(msg, Caption)]


def finish_lesson(teacher):
    teacher.send_json({"type": "control", "action": "end"})
    teacher.send_json({"type": "recording", "on": False})


def remaining_captions(socket):
    messages = []
    wait_for(socket, RecordingState, on=False, received=messages)
    return [(msg.seq, msg.text) for msg in messages if isinstance(msg, Caption)]


def test_switch_refreshes_retained_history_and_saves_new_translations(tmp_path):
    mt = FakeTranslationEngine(["en", "zh", "ko"])
    with classroom(tmp_path, mt) as (teacher, student, peer):
        before_barrier = switch_language(student, "zh")
        finish_lesson(teacher)
        assert before_barrier + remaining_captions(student) == [
            (1, f"[zh] {PHRASE_1000}"), (2, f"[zh] {PHRASE_2000}"),
        ]
        assert remaining_captions(peer) == []

    (path,) = tmp_path.glob("*/transcript.jsonl")
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [entry["translations"]["zh"]["text"] for entry in entries] == [
        f"[zh] {PHRASE_1000}", f"[zh] {PHRASE_2000}",
    ]


def test_switch_discards_previous_history_request(tmp_path):
    mt = HeldRefresh()
    with classroom(tmp_path, mt) as (teacher, student, _):
        try:
            switch_language(student, "zh")
            assert mt.started.wait(3)
            before_barrier = switch_language(student, "ko")
        finally:
            mt.release.set()
        finish_lesson(teacher)
        assert before_barrier + remaining_captions(student) == [
            (1, f"[ko] {PHRASE_1000}"), (2, f"[ko] {PHRASE_2000}"),
        ]
        assert (PHRASE_2000, "zh") not in mt.calls


def test_switching_back_refreshes_even_if_the_previous_result_was_queued(tmp_path):
    mt = HeldRefresh("ko")
    with classroom(tmp_path, mt) as (teacher, student, _):
        chinese = switch_language(student, "zh")
        while len(chinese) < 2:
            caption = wait_for(student, Caption)
            chinese.append((caption.seq, caption.text))
        try:
            switch_language(student, "ko")
            assert mt.started.wait(3)
            before_barrier = switch_language(student, "zh")
        finally:
            mt.release.set()
        finish_lesson(teacher)
        updated = before_barrier + remaining_captions(student)
        assert [caption for caption in updated if caption[1].startswith("[zh] ")] == chinese


@pytest.mark.parametrize("watermark", [1, 128])
def test_live_caption_precedes_queued_history_and_pressure_discards_it(tmp_path, watermark):
    mt = HeldRefresh()
    with classroom(tmp_path, mt, watermark=watermark) as (teacher, student, peer):
        try:
            switch_language(student, "zh")
            assert mt.started.wait(3)
            send_utterance(teacher, 3000)
            wait_for(teacher, Stats, mt_queue_depth=2)
        finally:
            mt.release.set()
        finish_lesson(teacher)
        assert remaining_captions(peer) == [(3, f"[en] {PHRASE_3000}")]
        expected = [(3, f"[zh] {PHRASE_3000}")]
        if watermark == 128:
            expected = [(1, f"[zh] {PHRASE_1000}"), *expected, (2, f"[zh] {PHRASE_2000}")]
        assert remaining_captions(student) == expected
