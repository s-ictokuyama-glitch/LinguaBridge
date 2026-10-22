"""不変条件: recording OFF = 0 persisted audio（#23）。

「音声はディスクに残さない」は依頼者の絶対要件で、デバッグ用の WAV 書き出しを
1行足すだけで壊れる。ASRエンジン差し替え（#28）は入力PCMの扱いを触るので
特に危ない。記録OFFの授業1回分で永続化バイト数がゼロであること、
記録ONでも音声は1バイトも書かれないことを主張する。
"""

from __future__ import annotations

import wave
from pathlib import Path

from starlette.testclient import TestClient

from server.asr.fake_engine import FakeASREngine
from server.config import RecordingConfig
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.fs_guard import AUDIO_SUFFIXES, added_files, guard_audio_writers, snapshot
from tests.integration.test_ws_boundary import (
    join_student,
    join_teacher,
    send_utterance,
    start_session,
)


def make_app(out_dir: Path, *, default_on: bool):
    config = make_ws_test_config()
    config.recording = RecordingConfig(default_on=default_on, out_dir=str(out_dir))
    return create_app(
        config,
        asr_engine=FakeASREngine(),
        mt_engine=FakeTranslationEngine(["en", "zh"]),
        join_code=JOIN_CODE,
    )


def run_lesson(app, *, utterances=(1000, 2000, 3000)) -> None:
    """先生が参加 → 配信 → 発話 → 終了 まで通す（記録の有無は app 側の設定）。"""
    with TestClient(app) as client:
        with (
            client.websocket_connect("/ws") as teacher,
            client.websocket_connect("/ws") as student,
        ):
            join_teacher(teacher)
            join_student(student, "en")
            start_session(teacher)
            student.receive_json()  # session live

            for key in utterances:
                send_utterance(teacher, key=key)
            finals = 0
            while finals < len(utterances):
                if teacher.receive_json()["type"] == "asr_final":
                    finals += 1
            teacher.send_json({"type": "control", "action": "end"})
            teacher.receive_json()


class TestGuardItself:
    """ガードが本当に検出できることを先に固定する。"""

    def test_wave_write_is_detected(self, tmp_path):
        with guard_audio_writers() as guard:
            with wave.open(str(tmp_path / "debug.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 16000)
        assert guard.calls, "wave.open の書き込みが記録されていません"

    def test_raw_pcm_dump_is_detected(self, tmp_path):
        with guard_audio_writers() as guard:
            (tmp_path / "debug.pcm").write_bytes(b"\x00\x00" * 100)
        assert guard.calls

    def test_text_write_is_not_flagged(self, tmp_path):
        with guard_audio_writers() as guard:
            (tmp_path / "transcript.jsonl").write_bytes(b"{}\n")
        assert guard.calls == []

    def test_snapshot_detects_added_and_grown_files(self, tmp_path):
        before = snapshot(tmp_path)
        (tmp_path / "new.txt").write_text("a")
        after = snapshot(tmp_path)
        assert added_files(before, after) == {"new.txt": 1}

    def test_restores_patched_apis_on_exit(self):
        before_wave, before_write = wave.open, Path.write_bytes
        with guard_audio_writers():
            assert wave.open is not before_wave
        assert wave.open is before_wave
        assert Path.write_bytes is before_write


class TestRecordingOffPersistsNothing:
    def test_no_file_is_created_anywhere_when_recording_off(self, tmp_path):
        out_dir = tmp_path / "sessions"
        before = snapshot(tmp_path)
        with guard_audio_writers() as guard:
            run_lesson(make_app(out_dir, default_on=False))
        after = snapshot(tmp_path)

        assert added_files(before, after) == {}, "記録OFFなのにファイルが作られました"
        guard.assert_no_audio_written()

    def test_out_dir_is_not_even_created_when_recording_off(self, tmp_path):
        out_dir = tmp_path / "sessions"
        run_lesson(make_app(out_dir, default_on=False))
        assert not out_dir.exists()


class TestRecordingOnPersistsTextOnly:
    def test_recording_on_writes_transcripts_but_no_audio(self, tmp_path):
        out_dir = tmp_path / "sessions"
        before = snapshot(tmp_path)
        with guard_audio_writers() as guard:
            run_lesson(make_app(out_dir, default_on=True))
        added = added_files(before, snapshot(tmp_path))

        assert added, "記録ONなのに何も書かれていません（テストが壊れている）"
        guard.assert_no_audio_written()
        offenders = [
            name for name in added if Path(name).suffix.lower() in AUDIO_SUFFIXES
        ]
        assert offenders == [], f"記録ONで音声ファイルが書かれました: {offenders}"
        assert all(
            Path(name).suffix in {".jsonl", ".md"} for name in added
        ), f"想定外の形式が書かれました: {sorted(added)}"

    def test_persisted_bytes_are_decodable_text(self, tmp_path):
        """書き出されたものが本当にテキストであること（拡張子偽装の音声を排除）。"""
        out_dir = tmp_path / "sessions"
        run_lesson(make_app(out_dir, default_on=True))
        written = [p for p in out_dir.rglob("*") if p.is_file()]
        assert written
        for path in written:
            path.read_text(encoding="utf-8")  # デコードできなければ UnicodeDecodeError
