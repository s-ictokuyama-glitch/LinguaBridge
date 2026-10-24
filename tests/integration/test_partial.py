"""partial字幕と「発話中」インジケーターのWS境界テスト（#29）。

partial は**速く見せるためだけの機能**なので、確定字幕の経路に一切影響しないことが
すべての前提になる。ここで固定するのはその不変条件:

    - partial は**生徒に届かない**（未翻訳の日本語を生徒に見せない）
    - partial は**翻訳を増やさない**（Hy-MT2 の呼び出し回数が変わらない）
    - partial は**履歴に載らない**（再接続の差分復元に出てこない）
    - partial は **seq を消費しない**
    - `partial.enabled: false` で `turn.partial` が1件も出ない（= #29 以前と同一）

「発話中」は `speaking` フレームで全クライアントへ届く。
"""

from __future__ import annotations

import numpy as np
import pytest
from starlette.testclient import TestClient

from server import ws_protocol as proto
from server.publication import Publication
from server.asr.fake_engine import FakeASREngine
from server.config import PartialConfig
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.helpers import chunks, silence_pcm, speech_pcm, wait_for
from tests.integration.test_ws_boundary import join_student, join_teacher

PHRASE = FakeASREngine.PHRASES[2000]  # 「光合成には日光が必要です。」= StrongEnd


def make_app(*, enabled: bool = True, speaking_indicator: bool = True):
    config = make_ws_test_config()
    config.partial = PartialConfig(
        enabled=enabled,
        # フェイクの音源は 100ms フレームなので、interim は 1フレーム＝100ms 相当。
        # min_silence_ms(500) より短ければよい
        interim_silence_ms=96,
        min_interim_audio_s=0.3,
        speaking_indicator=speaking_indicator,
    )
    asr = FakeASREngine()
    mt = FakeTranslationEngine(["en"])
    app = create_app(config, Publication(config.server), asr_engine=asr, mt_engine=mt, join_code=JOIN_CODE)
    return app, asr, mt


def utterance_with_a_mid_pause(teacher) -> None:
    """文中に 300ms の間を挟んだ1発話。

    300ms は interim_silence_ms(96) 以上・min_silence_ms(500) 未満なので、
    **Segment は割れないが interim は出る**位置。partial の価値が出るのはこの区間だけ。
    """
    pcm = np.concatenate(
        [
            speech_pcm(2000, 0.8),
            silence_pcm(0.3),  # 文中の息継ぎ: interim の引き金
            speech_pcm(2000, 0.8),
            silence_pcm(1.5),  # 発話終了 → Segment 確定 → TurnBoundary
        ]
    )
    for chunk in chunks(pcm):
        teacher.send_bytes(chunk)


def start(teacher) -> None:
    teacher.send_json({"type": "control", "action": "start"})
    wait_for(teacher, proto.SessionStateMsg, state="live")


class TestPartialReachesOnlyTheTeacher:
    def test_teacher_sees_a_partial_before_the_final(self):
        app, _, _ = make_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                start(teacher)
                utterance_with_a_mid_pause(teacher)
                frames: list[proto.ServerMessage] = []
                final = wait_for(teacher, proto.AsrFinal, received=frames)

        partials = [f for f in frames if isinstance(f, proto.TurnPartial)]
        assert partials, "文中の間があるのに partial が1件も出ていない"
        assert all(p.ja for p in partials)
        # 確定と同じ Turn を指していること（先生UIはこれで暫定行を差し替える）
        assert {p.turn_id for p in partials} == {final.turn_id}
        # revision は turn 内で単調増加
        revisions = [p.revision for p in partials]
        assert revisions == sorted(revisions) and len(set(revisions)) == len(revisions)

    def test_students_never_receive_a_partial(self):
        app, _, _ = make_app()
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as teacher,
                client.websocket_connect("/ws") as student,
            ):
                join_student(student, "en")
                join_teacher(teacher)
                start(teacher)
                utterance_with_a_mid_pause(teacher)
                student_frames: list[proto.ServerMessage] = []
                caption = wait_for(student, proto.Caption, received=student_frames)

        assert not any(isinstance(f, proto.TurnPartial) for f in student_frames)
        # 生徒に届いた日本語は確定字幕に付く原文だけ
        assert caption.ja == PHRASE

    def test_partial_is_disabled_by_default_config(self):
        """`partial.enabled: false` は #29 以前と同一の挙動（turn.partial がゼロ件）。"""
        app, _, _ = make_app(enabled=False)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                start(teacher)
                utterance_with_a_mid_pause(teacher)
                frames: list[proto.ServerMessage] = []
                wait_for(teacher, proto.AsrFinal, received=frames)

        assert not any(isinstance(f, proto.TurnPartial) for f in frames)


class TestPartialDoesNotTouchTheConfirmedPath:
    """partial は「速く見せる」だけの機能。確定字幕の経路を1ミリも変えてはいけない。"""

    @pytest.mark.parametrize("enabled", [False, True])
    def test_translation_count_is_the_same_with_and_without_partial(self, enabled: bool):
        app, _, mt = make_app(enabled=enabled)
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as teacher,
                client.websocket_connect("/ws") as student,
            ):
                join_student(student, "en")
                join_teacher(teacher)
                start(teacher)
                utterance_with_a_mid_pause(teacher)
                wait_for(student, proto.Caption)

        # partial は翻訳へ流れない。1発話 = 1言語 = 翻訳1回のまま
        assert mt.calls == [(PHRASE, "en")]

    def test_partial_does_not_consume_a_seq(self):
        app, _, _ = make_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                start(teacher)
                utterance_with_a_mid_pause(teacher)
                final = wait_for(teacher, proto.AsrFinal)

        assert final.seq == 1  # partial が何回出ても seq は1発話ぶんしか進まない

    def test_partial_does_not_enter_the_history(self):
        """再接続した生徒が復元するのは確定字幕だけ。partial は履歴に載らない。"""
        app, _, _ = make_app()
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as teacher,
                client.websocket_connect("/ws") as student,
            ):
                join_student(student, "en")
                join_teacher(teacher)
                start(teacher)
                utterance_with_a_mid_pause(teacher)
                wait_for(student, proto.Caption)

            with client.websocket_connect("/ws") as rejoined:
                joined = join_student(rejoined, "en", last_seq=0)
                assert joined.seq_head == 1  # partial のぶん増えていない
                frames: list[proto.ServerMessage] = []
                restored = wait_for(rejoined, proto.Caption, received=frames)
                assert restored.seq == 1 and restored.ja == PHRASE
                assert not any(isinstance(f, proto.TurnPartial) for f in frames)


class TestSpeakingIndicator:
    def test_students_are_told_when_the_teacher_starts_and_stops_speaking(self):
        app, _, _ = make_app()
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as teacher,
                client.websocket_connect("/ws") as student,
            ):
                join_student(student, "en")
                join_teacher(teacher)
                start(teacher)
                utterance_with_a_mid_pause(teacher)
                frames: list[proto.ServerMessage] = []
                wait_for(student, proto.Speaking, on=True, received=frames)
                wait_for(student, proto.Speaking, on=False, received=frames)
                wait_for(student, proto.Caption, received=frames)

        speaking = [f.on for f in frames if isinstance(f, proto.Speaking)]
        assert speaking, "「発話中」が1件も届いていない"
        assert speaking[0] is True
        # 同じ値が連続しない = 変化時にだけ送っている（毎フレーム送っていない）
        assert all(a != b for a, b in zip(speaking, speaking[1:]))

    def test_indicator_can_be_turned_off_independently_of_partial(self):
        """インジケーターは ASR を1回も増やさないので partial とは別の設定にしてある。"""
        app, _, _ = make_app(enabled=True, speaking_indicator=False)
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as teacher,
                client.websocket_connect("/ws") as student,
            ):
                join_student(student, "en")
                join_teacher(teacher)
                start(teacher)
                utterance_with_a_mid_pause(teacher)
                frames: list[proto.ServerMessage] = []
                wait_for(student, proto.Caption, received=frames)

        assert not any(isinstance(f, proto.Speaking) for f in frames)

    def test_indicator_clears_when_the_session_pauses_mid_utterance(self):
        """**発話の途中で**止める。発話が終わってから止めても ON は既に畳まれている。"""
        app, _, _ = make_app()
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as teacher,
                client.websocket_connect("/ws") as student,
            ):
                join_student(student, "en")
                join_teacher(teacher)
                start(teacher)
                # 発話を閉じる無音を送らないまま一時停止する
                for chunk in chunks(speech_pcm(2000, 0.8)):
                    teacher.send_bytes(chunk)
                wait_for(student, proto.SessionStateMsg, state="live")
                frames: list[proto.ServerMessage] = []
                wait_for(student, proto.Speaking, on=True, received=frames)
                teacher.send_json({"type": "control", "action": "pause"})
                wait_for(student, proto.Speaking, on=False, received=frames)
                wait_for(student, proto.SessionStateMsg, state="paused", received=frames)

        speaking = [f for f in frames if isinstance(f, proto.Speaking)]
        assert [f.on for f in speaking] == [True, False]
        assert isinstance(frames[-1], proto.SessionStateMsg)


class TestPartialNeverQueuesBehindAFinal:
    """**partial は final の後ろに並ばない**（#29 の中心的な不変条件）。

    ASR は単一スレッドなので、interim が確定 Segment の前に入るとその発話の
    確定字幕がまるごと遅れる。partial は「速く見せる」ための機能なので、
    確定を遅らせたら本末転倒になる。そこで ASR待ちが空のときだけ積む。
    """

    def test_interims_are_dropped_while_the_asr_queue_is_busy(self):
        app, _, _ = make_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                start(teacher)
                # 間を置かずに発話を連投する。ASR待ちが空にならない窓ができ、
                # そこに掛かった interim は捨てられる
                for _ in range(4):
                    utterance_with_a_mid_pause(teacher)
                # 4件とも確定する。ここが返ること自体が「partial の抑制は確定字幕に
                # 影響しない」の検証で、返らなければ interim が確定を押しのけている
                finals = [wait_for(teacher, proto.AsrFinal) for _ in range(4)]

            assert [f.seq for f in finals] == [1, 2, 3, 4]
            # 捨てた interim を黙って消していない（数えている）
            pipeline = app.state.pipeline
            assert pipeline.interims_skipped + pipeline.partials_sent > 0
