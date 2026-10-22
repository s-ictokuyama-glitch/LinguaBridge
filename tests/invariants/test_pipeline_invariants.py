"""不変条件: 推論回数は生徒数に依存しない（#23）。

LinguaBridge の遅延予算は「先生の1発話につき ASR 1回・アクティブ言語ごとに翻訳1回」
という前提で成り立っている。生徒が増えると推論が増える実装になった瞬間、
教室規模で破綻する。P0是正（#25）・区切り変更（#27）・partial導入（#29）・
エンジン差し替え（#28）はいずれもこの経路を触るので、ここで固定する。

シームは3つだけ:
    - FakeASREngine.calls   （ASR呼び出し回数）
    - FakeTranslationEngine.calls （翻訳の (原文, 言語) 列）
    - WS境界（先生の asr_final / 生徒の caption）
パイプラインの内部構造は一切触らない。
"""

from __future__ import annotations

import contextlib

import pytest
from starlette.testclient import TestClient

from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, Language, MtConfig, PartialConfig, VadConfig
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE
from tests.integration.test_ws_boundary import (
    PHRASE_1000,
    PHRASE_2000,
    PHRASE_3000,
    join_student,
    join_teacher,
    send_utterance,
    start_session,
)

FIVE_LANGS = ["en", "zh", "pt", "vi", "ko"]


def make_app(langs: list[str]):
    config = AppConfig(
        vad=VadConfig(engine="energy", threshold=300),
        languages=[Language(code=code, label=code) for code in langs],
    )
    asr = FakeASREngine()
    mt = FakeTranslationEngine(langs)
    app = create_app(config, asr_engine=asr, mt_engine=mt, join_code=JOIN_CODE)
    return app, asr, mt


def await_finals(teacher, count: int) -> list[str]:
    """先生の asr_final を count 件受けて処理完了を同期する。確定した日本語を返す。"""
    finals: list[str] = []
    while len(finals) < count:
        msg = teacher.receive_json()
        if msg["type"] == "asr_final":
            finals.append(msg["ja"])
    return finals


def drain_mt_queue(teacher, mt: FakeTranslationEngine, expected: int) -> None:
    """翻訳がすべて済むまで先生側のメッセージを消費して待つ。

    asr_final は翻訳より先に届くため、asr_final だけでは翻訳完了を同期できない。
    """
    for _ in range(200):
        if len(mt.calls) >= expected:
            return
        with contextlib.suppress(Exception):
            teacher.receive_json()
    raise AssertionError(f"翻訳が {expected} 件に達しませんでした（{len(mt.calls)} 件）")


class TestOneTeacherStreamOneAsrPipeline:
    """`1 teacher stream = 1 ASR pipeline` — 生徒が何人いても ASR は発話数ぶんだけ。"""

    @pytest.mark.parametrize("student_count", [1, 10, 20, 40])
    def test_asr_calls_equal_utterance_count_regardless_of_students(self, student_count):
        app, asr, _mt = make_app(["en"])
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                with contextlib.ExitStack() as stack:
                    students = [
                        stack.enter_context(client.websocket_connect("/ws"))
                        for _ in range(student_count)
                    ]
                    for student in students:
                        join_student(student, "en")
                    start_session(teacher)

                    for key in (1000, 2000, 3000):
                        send_utterance(teacher, key=key)
                    finals = await_finals(teacher, 3)

        assert finals == [PHRASE_1000, PHRASE_2000, PHRASE_3000]
        assert len(asr.calls) == 3, (
            f"生徒 {student_count} 人で ASR が {len(asr.calls)} 回呼ばれました（期待: 3回）"
        )

    def test_no_asr_when_nobody_is_listening(self):
        """生徒0人でも先生の発話は ASR される（先生の文字起こし表示は生徒に依存しない）。"""
        app, asr, mt = make_app(["en"])
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                start_session(teacher)
                send_utterance(teacher, key=1000)
                assert await_finals(teacher, 1) == [PHRASE_1000]
        assert len(asr.calls) == 1
        assert mt.calls == [], "選択者0名なのに翻訳が走りました"


class TestOneInferencePerActiveLanguage:
    """`15 English students = 1 English inference` — 同一言語の人数で翻訳は増えない。"""

    @pytest.mark.parametrize("student_count", [1, 5, 15])
    def test_same_language_students_share_one_translation(self, student_count):
        app, _asr, mt = make_app(["en"])
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                with contextlib.ExitStack() as stack:
                    for _ in range(student_count):
                        join_student(
                            stack.enter_context(client.websocket_connect("/ws")), "en"
                        )
                    start_session(teacher)
                    send_utterance(teacher, key=2000)
                    await_finals(teacher, 1)
                    drain_mt_queue(teacher, mt, expected=1)

        assert mt.calls == [(PHRASE_2000, "en")], (
            f"英語の生徒 {student_count} 人で翻訳が {len(mt.calls)} 回走りました（期待: 1回）"
        )

    @pytest.mark.parametrize("lang_count", [1, 2, 3, 5])
    def test_translation_count_tracks_active_languages_not_students(self, lang_count):
        """1発話あたりの翻訳回数 = アクティブ言語数。各言語に3人ずつ座らせる。"""
        langs = FIVE_LANGS[:lang_count]
        app, asr, mt = make_app(FIVE_LANGS)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                with contextlib.ExitStack() as stack:
                    for lang in langs:
                        for _ in range(3):
                            join_student(
                                stack.enter_context(client.websocket_connect("/ws")), lang
                            )
                    start_session(teacher)
                    send_utterance(teacher, key=2000)
                    await_finals(teacher, 1)
                    drain_mt_queue(teacher, mt, expected=lang_count)

        assert len(asr.calls) == 1
        assert sorted(lang for _text, lang in mt.calls) == sorted(langs)
        assert all(text == PHRASE_2000 for text, _lang in mt.calls)

    def test_language_with_no_listener_is_not_translated(self):
        """設定にあっても選択者0名の言語には翻訳ジョブが出ない。"""
        app, _asr, mt = make_app(FIVE_LANGS)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                with client.websocket_connect("/ws") as student:
                    join_student(student, "vi")
                    start_session(teacher)
                    student.receive_json()  # session live
                    send_utterance(teacher, key=1000)
                    await_finals(teacher, 1)
                    drain_mt_queue(teacher, mt, expected=1)

        assert [lang for _text, lang in mt.calls] == ["vi"]


class TestOneFinalizedUtteranceOneTranslationSource:
    """`1 finalized utterance = 1 translation source` — 確定していない文字列は翻訳に流れない。

    partial 字幕（#29）はまだ実装されていないが、導入時にこの不変条件が
    最初に壊れる。翻訳へ渡された原文が「先生に asr_final として通知された文」の
    集合に一致することを主張しておけば、partial の混入は必ず赤で出る。
    """

    def test_every_translated_source_was_a_finalized_utterance(self):
        app, _asr, mt = make_app(["en", "zh"])
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                with (
                    client.websocket_connect("/ws") as en,
                    client.websocket_connect("/ws") as zh,
                ):
                    join_student(en, "en")
                    join_student(zh, "zh")
                    start_session(teacher)
                    en.receive_json()
                    zh.receive_json()

                    for key in (1000, 2000, 3000):
                        send_utterance(teacher, key=key)
                    finals = await_finals(teacher, 3)
                    drain_mt_queue(teacher, mt, expected=6)

        translated_sources = [text for text, _lang in mt.calls]
        assert set(translated_sources) == set(finals), (
            "確定発話に無い文字列が翻訳へ流れました（partial 混入の疑い）"
        )
        # 発話3件 × 言語2 = 6回。重複翻訳も欠落も無い
        assert len(mt.calls) == 6
        assert sorted(mt.calls) == sorted(
            (text, lang) for text in finals for lang in ("en", "zh")
        )

    def test_dropped_utterance_is_never_translated(self):
        """幻覚フィルタで破棄された発話は確定もされず翻訳もされない。"""
        app, asr, mt = make_app(["en"])
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                with client.websocket_connect("/ws") as student:
                    join_student(student, "en")
                    start_session(teacher)
                    student.receive_json()  # session live

                    send_utterance(teacher, key=4000)  # 既知の幻覚フレーズ
                    send_utterance(teacher, key=1000)
                    assert await_finals(teacher, 1) == [PHRASE_1000]
                    drain_mt_queue(teacher, mt, expected=1)

        assert len(asr.calls) == 2, "ASR は2回呼ばれる（フィルタは ASR の後段）"
        assert mt.calls == [(PHRASE_1000, "en")], "破棄された発話が翻訳へ流れました"


class TestRepeatedUtteranceIsTranslatedOnce:
    """`同じ原文 = 1回の推論` — 発話をまたぐ翻訳キャッシュ（#26 B-4）。

    授業では「はい」「もう一度言います」のような繰り返しが多い。2回目以降で
    Hy-MT2 を叩かないことと、**それでも字幕は必ず届くこと**を対で固定する。
    キャッシュが字幕を飲み込む壊れ方は、推論回数だけ見ていると見つからない。
    """

    def test_second_occurrence_does_not_call_the_engine(self):
        app, _asr, mt = make_app(["en"])
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                with client.websocket_connect("/ws") as student:
                    join_student(student, "en")
                    start_session(teacher)
                    student.receive_json()  # session live

                    send_utterance(teacher, key=1000)
                    send_utterance(teacher, key=1000)  # 同一原文
                    captions = [student.receive_json() for _ in range(2)]

        assert mt.calls == [(PHRASE_1000, "en")], (
            f"同一原文で翻訳が {len(mt.calls)} 回走りました（期待: 1回）"
        )
        # 推論を省いても字幕は2件とも届く（seq は別なので生徒は2枚のカードを見る）
        assert [c["ja"] for c in captions] == [PHRASE_1000, PHRASE_1000]
        assert [c["text"] for c in captions] == [f"[en] {PHRASE_1000}"] * 2
        assert captions[0]["seq"] != captions[1]["seq"]

    def test_cache_does_not_leak_across_languages(self):
        """英語のキャッシュが中国語の字幕に出てこない（キーの取り違えの検出）。"""
        app, _asr, mt = make_app(["en", "zh"])
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                with (
                    client.websocket_connect("/ws") as en,
                    client.websocket_connect("/ws") as zh,
                ):
                    join_student(en, "en")
                    join_student(zh, "zh")
                    start_session(teacher)
                    en.receive_json()
                    zh.receive_json()

                    send_utterance(teacher, key=1000)
                    send_utterance(teacher, key=1000)
                    en_caps = [en.receive_json() for _ in range(2)]
                    zh_caps = [zh.receive_json() for _ in range(2)]

        assert sorted(mt.calls) == sorted([(PHRASE_1000, "en"), (PHRASE_1000, "zh")])
        assert {c["text"] for c in en_caps} == {f"[en] {PHRASE_1000}"}
        assert {c["text"] for c in zh_caps} == {f"[zh] {PHRASE_1000}"}


class TestCacheCanBeDisabled:
    """`mt.cache_size = 0` で素の動作に戻せる（キャッシュを疑うときの切り分け手段）。"""

    def test_disabled_cache_translates_every_occurrence(self):
        config = AppConfig(
            vad=VadConfig(engine="energy", threshold=300),
            languages=[Language(code="en", label="en")],
            mt=MtConfig(engine="fake", cache_size=0),
        )
        asr = FakeASREngine()
        mt = FakeTranslationEngine(["en"])
        app = create_app(config, asr_engine=asr, mt_engine=mt, join_code=JOIN_CODE)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                with client.websocket_connect("/ws") as student:
                    join_student(student, "en")
                    start_session(teacher)
                    student.receive_json()

                    send_utterance(teacher, key=1000)
                    send_utterance(teacher, key=1000)
                    for _ in range(2):
                        student.receive_json()

        assert mt.calls == [(PHRASE_1000, "en")] * 2


class TestPartialDoesNotAddInference:
    """partial は ASR を増やすが、**翻訳は1回も増やさない**（#29）。

    #24 で分かったとおり ASR は固定費が支配的で、partial は同じ音声に対して固定費を
    もう一度払う設計になっている。#28 でその固定費が 1.088s → 0.027s になったから
    成立しているだけで、**翻訳側にまで漏れたら教室規模で破綻する**
    （Hy-MT2 は1件 850ms 前後・単一スレッド）。ここで漏れを止める。
    """

    @staticmethod
    def _app_with_partial(enabled: bool):
        config = AppConfig(
            vad=VadConfig(engine="energy", threshold=300),
            languages=[Language(code="en", label="en"), Language(code="zh", label="zh")],
            partial=PartialConfig(
                enabled=enabled, interim_silence_ms=96, min_interim_audio_s=0.3
            ),
        )
        asr = FakeASREngine()
        mt = FakeTranslationEngine(["en", "zh"])
        return create_app(config, asr_engine=asr, mt_engine=mt, join_code=JOIN_CODE), asr, mt

    @pytest.mark.parametrize("enabled", [False, True])
    def test_translation_count_is_unchanged_by_partial(self, enabled: bool):
        app, _asr, mt = self._app_with_partial(enabled)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                with contextlib.ExitStack() as stack:
                    for lang in ("en", "en", "zh"):
                        join_student(
                            stack.enter_context(client.websocket_connect("/ws")), lang
                        )
                    start_session(teacher)
                    send_utterance(teacher, key=2000)
                    await_finals(teacher, 1)
                    drain_mt_queue(teacher, mt, expected=2)

        assert sorted(mt.calls) == [(PHRASE_2000, "en"), (PHRASE_2000, "zh")], (
            f"partial={enabled} で翻訳が {len(mt.calls)} 回走りました（期待: アクティブ言語数の2回）"
        )

