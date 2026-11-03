"""授業記録（イシュー#18）のWS境界＋ファイル出力テスト。

記録の有無・形式・インジケーター・プライバシーをWS境界と実ファイルで検証する。
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from contextlib import asynccontextmanager

import pytest
import websockets
from starlette.testclient import TestClient

from server import event_loop, main
from server.publication import Publication
from server.asr.fake_engine import FakeASREngine
from server.config import RecordingConfig
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from server.serving import ListenerSpec, start_listeners
from server.ws_protocol import AsrFinal, Caption, ErrorMsg, RecordingState, SessionStateMsg
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.helpers import chunks, speech_pcm, wait_for
from tests.integration.test_ws_boundary import (
    PHRASE_1000,
    PHRASE_2000,
    PHRASE_3000,
    join_student,
    join_teacher,
    send_utterance,
    start_session,
)
from tests.listeners import receive


def make_app(tmp_path, *, default_on=False, asr=None, mt=None,
             strategy="simple", recording_wait_s=30):
    config = make_ws_test_config()
    config.recording = RecordingConfig(default_on=default_on, out_dir=str(tmp_path))
    config.turn.strategy = strategy
    return create_app(
        config, Publication(config.server),
        asr_engine=asr or FakeASREngine(),
        mt_engine=mt or FakeTranslationEngine(["en", "zh"]),
        join_code=JOIN_CODE,
        recording_wait_s=recording_wait_s,
    )


def set_recording(teacher, on: bool) -> None:
    teacher.send_json({"type": "recording", "on": on})


class InferenceGate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def hold(self):
        self.entered.set()
        if not self.release.wait(4):
            raise TimeoutError("inference gate was not released")


class GatedASR(FakeASREngine):
    def __init__(self, key):
        super().__init__()
        self.key = key
        self.gate = InferenceGate()

    def transcribe(self, pcm16, sample_rate):
        result = super().transcribe(pcm16, sample_rate)
        if result.text == self.PHRASES[self.key]:
            self.gate.hold()
        return result


class GatedMT(FakeTranslationEngine):
    def __init__(self):
        super().__init__(["en"])
        self.gate = InferenceGate()

    def translate(self, text, target_lang):
        self.gate.hold()
        return super().translate(text, target_lang)


def transcript(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def wait_for_markdown(path, last_text):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").endswith(last_text + "\n"):
            return
        time.sleep(0.01)
    pytest.fail(f"{path.name} did not contain the final translation")


@pytest.mark.parametrize("strategy", ["simple", "morph"])
@pytest.mark.parametrize("restart", ["start", "pause"])
def test_late_asr_stays_with_ended_lesson(tmp_path, strategy, restart):
    asr = GatedASR(3000)
    app = make_app(tmp_path, default_on=True, asr=asr, strategy=strategy, recording_wait_s=0.1)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(teacher)
            start_session(teacher)
            send_utterance(teacher, 1000)
            wait_for(student, Caption, seq=1)
            for chunk in chunks(speech_pcm(3000, 0.4)):
                teacher.send_bytes(chunk)
            try:
                teacher.send_json({"type": "control", "action": "end"})
                wait_for(student, SessionStateMsg, state="ended")
                assert asr.gate.entered.wait(2)
                wait_for(teacher, ErrorMsg, code="recording_pending")
                (original,) = tmp_path.glob("*/transcript.jsonl")
                assert [entry["ja"] for entry in transcript(original)] == [PHRASE_1000]
                teacher.send_json({"type": "control", "action": restart})
                wait_for(teacher, ErrorMsg, code="recording_pending")
                with client.websocket_connect("/ws") as observer:
                    assert join_student(observer, "en").session_state == "ended"
            finally:
                asr.gate.release.set()

            wait_for(student, Caption, seq=2)
            wait_for_markdown(original.with_name("transcript.en.md"), f"[en] {PHRASE_3000}")
            assert [entry["ja"] for entry in transcript(original)] == [PHRASE_1000, PHRASE_3000]
            saved = {path.name: path.read_bytes() for path in original.parent.iterdir()}
            teacher.send_json({"type": "control", "action": restart})
            wait_for(teacher, SessionStateMsg, state="live" if restart == "start" else "paused")
            if restart == "pause":
                start_session(teacher)
            send_utterance(teacher, 2000)
            wait_for(student, Caption, seq=3)
            teacher.send_json({"type": "control", "action": "end"})
            set_recording(teacher, False)
            wait_for(teacher, RecordingState, on=False)
            (next_lesson,) = set(tmp_path.glob("*/transcript.jsonl")) - {original}
            assert [entry["ja"] for entry in transcript(next_lesson)] == [PHRASE_2000]
            assert saved == {path.name: path.read_bytes() for path in original.parent.iterdir()}


def test_late_mt_updates_provisional_recording(tmp_path):
    mt = GatedMT()
    app = make_app(tmp_path, default_on=True, mt=mt, recording_wait_s=0.1)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(teacher)
            start_session(teacher)
            send_utterance(teacher, 1000)
            try:
                wait_for(teacher, AsrFinal, seq=1)
                assert mt.gate.entered.wait(2)
                teacher.send_json({"type": "control", "action": "end"})
                wait_for(teacher, ErrorMsg, code="recording_pending")
                (saved,) = tmp_path.glob("*/transcript.jsonl")
                assert transcript(saved)[0]["translations"] == {}
            finally:
                mt.gate.release.set()
            wait_for(student, Caption, seq=1)
            wait_for_markdown(saved.with_name("transcript.en.md"), f"[en] {PHRASE_1000}")
            assert transcript(saved)[0]["translations"]["en"]["text"] == f"[en] {PHRASE_1000}"
            assert list(tmp_path.glob("*/transcript.jsonl")) == [saved]


@pytest.mark.parametrize("strategy", ["simple", "morph"])
def test_end_saves_both_pending_segments(tmp_path, strategy):
    asr = GatedASR(1000)
    app = make_app(tmp_path, default_on=True, asr=asr, strategy=strategy, recording_wait_s=0.1)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(teacher)
            start_session(teacher)
            try:
                send_utterance(teacher, 1000)
                assert asr.gate.entered.wait(2)
                for chunk in chunks(speech_pcm(2000, 0.4)):
                    teacher.send_bytes(chunk)
                teacher.send_json({"type": "control", "action": "end"})
                wait_for(teacher, ErrorMsg, code="recording_pending")
                assert list(tmp_path.iterdir()) == []
            finally:
                asr.gate.release.set()
            wait_for(student, Caption, seq=2)
            start_session(teacher)
            (saved,) = tmp_path.glob("*/transcript.jsonl")
            entries = transcript(saved)
            assert [entry["ja"] for entry in entries] == [PHRASE_1000, PHRASE_2000]
            assert all(entry["translations"]["en"]["text"] for entry in entries)


def test_recording_off_still_blocks_restart_while_old_asr_is_pending(tmp_path):
    asr = GatedASR(3000)
    app = make_app(tmp_path, asr=asr, strategy="morph", recording_wait_s=0.1)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(teacher)
            start_session(teacher)
            for chunk in chunks(speech_pcm(3000, 0.4)):
                teacher.send_bytes(chunk)
            try:
                teacher.send_json({"type": "control", "action": "end"})
                wait_for(student, SessionStateMsg, state="ended")
                assert asr.gate.entered.wait(2)
                teacher.send_json({"type": "control", "action": "start"})
                wait_for(teacher, ErrorMsg, code="recording_pending")
            finally:
                asr.gate.release.set()
            wait_for(student, Caption, seq=1)
            start_session(teacher)
            assert list(tmp_path.iterdir()) == []
            set_recording(teacher, True)
            send_utterance(teacher, 2000)
            wait_for(student, Caption, seq=2)
            teacher.send_json({"type": "control", "action": "end"})
            set_recording(teacher, False)
            wait_for(teacher, RecordingState, on=False)
            (saved,) = tmp_path.glob("*/transcript.jsonl")
            assert [entry["ja"] for entry in transcript(saved)] == [PHRASE_2000]


def test_failed_background_save_retries_in_the_provisional_directory(tmp_path):
    mt = GatedMT()
    app = make_app(tmp_path, default_on=True, mt=mt, recording_wait_s=0.1)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(teacher)
            start_session(teacher)
            send_utterance(teacher, 1000)
            try:
                assert mt.gate.entered.wait(2)
                teacher.send_json({"type": "control", "action": "end"})
                wait_for(teacher, ErrorMsg, code="recording_pending")
                (provisional,) = tmp_path.glob("*/transcript.jsonl")
                blocked = provisional.with_name("transcript.en.md")
                blocked.mkdir()
            finally:
                mt.gate.release.set()
            wait_for(teacher, ErrorMsg, code="recording_save_failed")
            teacher.send_json({"type": "control", "action": "start"})
            wait_for(teacher, ErrorMsg, code="recording_save_failed")
            blocked.rmdir()
            start_session(teacher)
            assert list(tmp_path.glob("*/transcript.jsonl")) == [provisional]
            assert transcript(provisional)[0]["translations"]["en"]["text"] == f"[en] {PHRASE_1000}"


@pytest.mark.parametrize("action", ["start", "pause"])
def test_two_teachers_waiting_to_restart_share_one_lesson_rotation(tmp_path, action):
    asr = GatedASR(3000)
    app = make_app(tmp_path, default_on=True, asr=asr, recording_wait_s=1)
    starts = queue.SimpleQueue()

    async def observed_app(scope, receive, send):
        async def observe_start():
            frame = await receive()
            if frame.get("text"):
                message = json.loads(frame["text"])
                if message.get("type") == "control" and message.get("action") in ("start", "pause"):
                    asyncio.get_running_loop().call_soon(starts.put, None)
            return frame
        await app(scope, observe_start, send)

    with TestClient(observed_app) as client:
        with client.websocket_connect("/ws") as old, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(old)
            start_session(old)
            starts.get(timeout=2)
            wait_for(student, SessionStateMsg, state="live")
            try:
                send_utterance(old, 3000)
                assert asr.gate.entered.wait(2)
                old.send_json({"type": "control", "action": "end"})
                wait_for(old, ErrorMsg, code="recording_pending")
                old.send_json({"type": "control", "action": "start"})
                starts.get(timeout=2)
                with client.websocket_connect("/ws") as teacher:
                    assert join_teacher(teacher).session_state == "ended"
                    teacher.send_json({"type": "control", "action": action})
                    starts.get(timeout=2)
                    asr.gate.release.set()

                    frames = []
                    set_recording(teacher, True)
                    wait_for(student, RecordingState, on=True, received=frames)
                    assert [msg.state for msg in frames
                            if isinstance(msg, SessionStateMsg) and msg.state != "ended"] == [
                        "live" if action == "start" else "paused"
                    ]
                    if action == "pause":
                        start_session(teacher)
                        wait_for(student, SessionStateMsg, state="live", received=frames)
                    send_utterance(teacher, 2000)
                    wait_for(student, Caption, seq=2, received=frames)
                    live_count = sum(isinstance(msg, SessionStateMsg) and msg.state == "live"
                                     for msg in frames)
                    assert live_count == 1
                    teacher.send_json({"type": "control", "action": "end"})
                    set_recording(teacher, False)
                    wait_for(teacher, RecordingState, on=False)
                    assert sorted([entry["seq"] for entry in transcript(path)]
                                  for path in tmp_path.glob("*/transcript.jsonl")) == [[1], [2]]
            finally:
                asr.gate.release.set()


@pytest.mark.parametrize("expires", [False, True])
def test_listener_shutdown_flushes_records_and_delivers_before_socket_close(
    tmp_path, monkeypatch, expires,
):
    asr = GatedASR(2000) if expires else FakeASREngine()
    app = make_app(tmp_path, default_on=True, asr=asr, recording_wait_s=0.1 if expires else 30)

    @asynccontextmanager
    async def loopback_listeners(app, _specs, **kwargs):
        specs = [ListenerSpec("127.0.0.1", 0, lifespan=True), ListenerSpec("127.0.0.1", 0)]
        async with start_listeners(app, specs, install_signals=False, **kwargs) as running:
            teacher_port, student_port = running.ports
            async with (websockets.connect(f"ws://127.0.0.1:{teacher_port}/ws") as teacher,
                        websockets.connect(f"ws://127.0.0.1:{student_port}/ws") as student):
                await teacher.send(json.dumps({"type": "join", "role": "teacher", "code": JOIN_CODE}))
                await student.send(json.dumps({"type": "join", "role": "student", "code": JOIN_CODE, "lang": "en"}))
                await receive(teacher, "joined")
                await receive(student, "joined")
                await teacher.send(json.dumps({"type": "control", "action": "start"}))
                await receive(student, "session", state="live")
                for chunk in chunks(speech_pcm(2000, 0.4)):
                    await teacher.send(chunk)
                await receive(student, "speaking", on=True)

                try:
                    running.stop()
                    await receive(teacher, "session", state="live")
                    await receive(teacher, "session", state="ended")
                    await receive(student, "session", state="ended")
                    if expires:
                        assert await asyncio.to_thread(asr.gate.entered.wait, 2)
                    else:
                        await receive(teacher, "asr_final", seq=1, ja=PHRASE_2000)
                        await receive(student, "caption", seq=1, ja=PHRASE_2000)
                    for ws in (teacher, student):
                        with pytest.raises(websockets.ConnectionClosed) as closed:
                            await asyncio.wait_for(ws.recv(), timeout=2)
                        assert closed.value.rcvd.code == 1012
                    if expires:
                        assert not any(task.get_name() == "recording-finalizer"
                                       for task in asyncio.all_tasks())
                finally:
                    if expires:
                        asr.gate.release.set()
            yield running

    monkeypatch.setattr("server.serving.start_listeners", loopback_listeners)
    event_loop.run(main._serve(app, open_browser=False))
    if expires:
        assert list(tmp_path.iterdir()) == []
        return
    (saved,) = tmp_path.glob("*/transcript.jsonl")
    (entry,) = transcript(saved)
    assert entry["ja"] == PHRASE_2000
    assert entry["translations"]["en"]["text"] == f"[en] {PHRASE_2000}"


def test_save_failure_keeps_teacher_connected_and_end_retries(tmp_path, caplog):
    destination = tmp_path / "recordings"
    destination.touch()
    app = make_app(destination, default_on=True)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(teacher)
            start_session(teacher)
            wait_for(student, SessionStateMsg, state="live")
            send_utterance(teacher, key=2000)
            wait_for(student, Caption)

            teacher.send_json({"type": "control", "action": "end"})
            wait_for(student, SessionStateMsg, state="ended")
            assert wait_for(teacher, ErrorMsg).code == "recording_save_failed"
            assert any(
                record.exc_info and isinstance(record.exc_info[1], FileExistsError)
                for record in caplog.records
            )

            teacher.send_json({"type": "control", "action": "start"})
            assert wait_for(teacher, ErrorMsg).code == "recording_save_failed"
            assert destination.is_file()

            destination.unlink()
            teacher.send_json({"type": "control", "action": "end"})
            set_recording(teacher, False)
            received = []
            wait_for(student, RecordingState, on=False, received=received)
            assert [msg.state for msg in received if isinstance(msg, SessionStateMsg)] == ["ended"]

            (transcript,) = destination.glob("*/transcript.jsonl")
            (line,) = transcript.read_text(encoding="utf-8").splitlines()
            assert json.loads(line)["ja"] == PHRASE_2000

            start_session(teacher)
            wait_for(student, SessionStateMsg, state="live")


class TestRecordingFiles:
    def test_writes_files_only_at_end_not_before(self, tmp_path):
        app = make_app(tmp_path)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
                join_student(student, "en")
                join_teacher(teacher)
                set_recording(teacher, True)
                wait_for(teacher, RecordingState, on=True)
                wait_for(student, RecordingState, on=True)
                start_session(teacher)
                wait_for(student, SessionStateMsg, state="live")
                send_utterance(teacher, key=2000)
                wait_for(student, Caption)
                # 授業中はまだ何も書き出さない
                assert list(tmp_path.iterdir()) == []
                teacher.send_json({"type": "control", "action": "end"})
                wait_for(student, SessionStateMsg, state="ended")

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
                wait_for(teacher, RecordingState, on=True)
                wait_for(student, RecordingState, on=True)
                start_session(teacher)
                wait_for(student, SessionStateMsg, state="live")
                send_utterance(teacher, key=2000)
                wait_for(student, Caption)
                teacher.send_json({"type": "control", "action": "end"})
                wait_for(student, SessionStateMsg, state="ended")

        out = list(tmp_path.iterdir())[0]
        entry = json.loads((out / "transcript.jsonl").read_text(encoding="utf-8").splitlines()[0])
        # 発話由来のキーのみ（接続元・クライアントID等が無い）
        assert set(entry.keys()) == {
            "seq", "created_at", "t_start", "t_end", "ja", "asr_ms", "translations"
        }
