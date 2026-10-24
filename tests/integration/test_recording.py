"""授業記録（イシュー#18）のWS境界＋ファイル出力テスト。

記録の有無・形式・インジケーター・プライバシーをWS境界と実ファイルで検証する。
"""

from __future__ import annotations

import json

from starlette.testclient import TestClient

from server.publication import Publication
from server.asr.fake_engine import FakeASREngine
from server.config import RecordingConfig
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.integration.test_ws_boundary import (
    PHRASE_2000,
    join_student,
    join_teacher,
    send_utterance,
    start_session,
)


def make_app(tmp_path, *, default_on=False):
    config = make_ws_test_config()
    config.recording = RecordingConfig(default_on=default_on, out_dir=str(tmp_path))
    return create_app(
        config, Publication(config.server),
        asr_engine=FakeASREngine(),
        mt_engine=FakeTranslationEngine(["en", "zh"]),
        join_code=JOIN_CODE,
    )


def set_recording(teacher, on: bool) -> None:
    teacher.send_json({"type": "recording", "on": on})


class TestRecordingFiles:
    def test_writes_files_only_at_end_not_before(self, tmp_path):
        app = make_app(tmp_path)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
                join_student(student, "en")
                join_teacher(teacher)
                set_recording(teacher, True)
                teacher.receive_json()  # recording broadcast (teacher)
                student.receive_json()  # recording broadcast (student)
                start_session(teacher)
                student.receive_json()  # session live
                send_utterance(teacher, key=2000)
                assert student.receive_json()["type"] == "caption"
                # 授業中はまだ何も書き出さない
                assert list(tmp_path.iterdir()) == []
                teacher.send_json({"type": "control", "action": "end"})
                assert student.receive_json() == {"type": "session", "state": "ended"}

        # 終了後、記録が保存されている
        dirs = list(tmp_path.iterdir())
        assert len(dirs) == 1
        out = dirs[0]
        assert (out / "transcript.jsonl").exists()
        assert (out / "transcript.ja.md").exists()
        assert (out / "transcript.en.md").exists()
        assert not (out / "transcript.zh.md").exists()  # zh選択者0名 → 訳文なし

        entry = json.loads((out / "transcript.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert entry["ja"] == PHRASE_2000
        assert entry["translations"]["en"]["text"] == f"[en] {PHRASE_2000}"

    def test_output_contains_no_student_info(self, tmp_path):
        app = make_app(tmp_path)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
                join_student(student, "en")
                join_teacher(teacher)
                set_recording(teacher, True)
                teacher.receive_json()
                student.receive_json()
                start_session(teacher)
                student.receive_json()
                send_utterance(teacher, key=2000)
                student.receive_json()
                teacher.send_json({"type": "control", "action": "end"})
                student.receive_json()

        out = list(tmp_path.iterdir())[0]
        entry = json.loads((out / "transcript.jsonl").read_text(encoding="utf-8").splitlines()[0])
        # 発話由来のキーのみ（接続元・クライアントID等が無い）
        assert set(entry.keys()) == {
            "seq", "created_at", "t_start", "t_end", "ja", "asr_ms", "translations"
        }
