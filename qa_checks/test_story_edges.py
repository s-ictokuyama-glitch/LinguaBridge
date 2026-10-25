"""Additional acceptance probes; explicit opt-in, not part of the existing suite.

These assertions intentionally expose current defects; do not xfail them or
modify production behavior merely to make an inventory/test task green.
"""

import asyncio
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from server.config import AppConfig, Language
from server.recorder import SessionRecorder
from tests.unit.test_recorder import make_utterance
from tests.unit.test_backpressure import make_pipeline, segment_of
from tests.integration.test_recording import make_app
from tests.integration.test_ws_boundary import join_student, join_teacher, send_utterance


def receive_kind(ws, kind, **fields):
    for _ in range(50):
        message = ws.receive_json()
        if message.get("type") == kind and all(message.get(k) == v for k, v in fields.items()):
            return message
    raise AssertionError(f"No {kind} frame matching {fields}")


def test_rec05_first_pending_utterance_is_drained_before_save(tmp_path):
    async def scenario():
        pipeline, roster = await make_pipeline()
        pipeline._recorder = SessionRecorder(tmp_path, ["en"])
        pipeline.set_recording(True)
        await pipeline._asr_queue.put(segment_of(1))
        finalizing = asyncio.create_task(pipeline.finalize_recording())
        try:
            await asyncio.sleep(0)
            prematurely_done = finalizing.done()
            # Deterministically finish the first queued ASR result.
            pipeline._recorder.add(make_utterance(1, "最後の説明。", {"en": "Last explanation."}))
            pipeline._asr_queue.get_nowait()
            pipeline._asr_queue.task_done()
            saved = await asyncio.wait_for(finalizing, 2)
            assert not prematurely_done, "終了処理が最初のASR待ちを排出せず戻った"
            assert saved and (saved / "transcript.jsonl").exists()
        finally:
            await pipeline.stop()
    asyncio.run(scenario())


def test_rec07_retry_after_write_error(tmp_path, monkeypatch):
    recorder = SessionRecorder(tmp_path, ["en"])
    recorder.add(make_utterance(1, "保存する内容。", {}))
    original = Path.mkdir
    def failing_mkdir(path, *args, **kwargs):
        if path.parent == tmp_path:
            raise OSError("simulated disk error")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", failing_mkdir)
        with pytest.raises(OSError, match="simulated"):
            recorder.write()
    assert recorder.write() is not None, "書込失敗後の再試行が_writtenで無視された"


def test_rec07_sessions_in_same_minute_do_not_overwrite(tmp_path):
    first = SessionRecorder(tmp_path, ["en"])
    first.add(make_utterance(1, "第一授業。", {}))
    first_dir = first.write()
    second = SessionRecorder(tmp_path, ["en"])
    second.add(make_utterance(1, "第二授業。", {}))
    second_dir = second.write()
    assert first_dir != second_dir, "同一分の別授業が同じファイルを上書きした"
    assert "第一授業" in (first_dir / "transcript.jsonl").read_text(encoding="utf-8")


def test_teach05_recording_survives_end_start_end(tmp_path):
    app = make_app(tmp_path, default_on=True)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(teacher)
            for key in [1000, 2000]:
                teacher.send_json({"type": "control", "action": "start"})
                receive_kind(student, "session", state="live")
                send_utterance(teacher, key=key)
                receive_kind(student, "caption")
                teacher.send_json({"type": "control", "action": "end"})
                receive_kind(student, "session", state="ended")
                # Ordered same-socket response is a barrier after end's save.
                teacher.send_json({"type": "recording", "on": True})
                receive_kind(student, "recording", on=True)
    entries = [json.loads(line) for file in tmp_path.rglob("transcript.jsonl")
               for line in file.read_text(encoding="utf-8").splitlines()]
    assert {entry["seq"] for entry in entries} == {1, 2}, "終了→開始後の授業が保存されない"


def test_rec06_toggle_uses_confirmation_time(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(teacher)
            teacher.send_json({"type": "control", "action": "start"})
            receive_kind(student, "session", state="live")
            for key, enabled in [(1000, False), (2000, True), (3000, False)]:
                teacher.send_json({"type": "recording", "on": enabled})
                receive_kind(student, "recording", on=enabled)
                send_utterance(teacher, key=key)
                receive_kind(student, "caption")
            teacher.send_json({"type": "control", "action": "end"})
            receive_kind(student, "session", state="ended")
    entries = [json.loads(line) for file in tmp_path.rglob("transcript.jsonl")
               for line in file.read_text(encoding="utf-8").splitlines()]
    assert [entry["seq"] for entry in entries] == [2]


def test_safe07_student_cannot_control_or_record_or_send_audio(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as student:
            join_student(student, "en")
            for action in ["start", "pause", "end"]:
                student.send_json({"type": "control", "action": action})
            student.send_json({"type": "recording", "on": True})
            send_utterance(student, key=2000)
            student.send_json({"type": "set_lang", "lang": "fr"})
            receive_kind(student, "error", code="bad_lang")
            assert app.state.lesson.state == "idle"
            assert app.state.lesson.recording is False
            assert app.state.pipeline.asr_calls == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("languages", [[], [Language(code="en", label="A"), Language(code="en", label="B")]])
def test_ops07_invalid_language_configuration(languages):
    with pytest.raises(ValueError):
        AppConfig(languages=languages)
