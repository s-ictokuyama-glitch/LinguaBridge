from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest
import websockets

from scripts.replay_client import Results, drain_teacher, run_student, run_teacher


JOINED = {
    "type": "joined",
    "seq_head": 0,
    "history_from": 1,
    "languages": [{"code": "en", "label": "English"}],
    "session_state": "idle",
}
STATS = {
    "type": "stats",
    "students": 1,
    "langs": {"en": 1},
    "queue_depth": 2,
    "median_delay_ms": 123,
}


def caption(seq: int, delay_ms: int = 123) -> dict:
    return {
        "type": "caption",
        "seq": seq,
        "ja": "こんにちは",
        "text": "Hello",
        "lang": "en",
        "delay_ms": delay_ms,
    }


class Socket:
    def __init__(self, messages: list[str | bytes | dict]):
        self.messages = iter(messages)
        self.sent: list[tuple[dict | bytes, int]] = []
        self.received = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()

    async def recv(self):
        try:
            message = next(self.messages)
        except StopIteration:
            raise StopAsyncIteration
        self.received += 1
        return json.dumps(message) if isinstance(message, dict) else message

    async def send(self, raw):
        self.sent.append((json.loads(raw) if isinstance(raw, str) else raw, self.received))


def connect_sockets(monkeypatch, *sockets: Socket) -> list[Socket]:
    remaining = iter(sockets)
    connected = []

    def connect(*args, **kwargs):
        ws = next(remaining)
        connected.append(ws)
        return ws

    monkeypatch.setattr(websockets, "connect", connect)
    return connected


def test_teacher_records_partial_final_stats_and_error() -> None:
    results = Results()
    ws = Socket([
        {"type": "turn.partial", "turn_id": 5, "revision": 1, "ja": "こん"},
        {"type": "turn.partial", "turn_id": 5, "revision": 2, "ja": "こんにちは"},
        {"type": "asr_final", "turn_id": 5, "seq": 1, "ja": "こんにちは", "asr_ms": 4},
        {"type": "asr_final", "seq": 2, "ja": "次", "asr_ms": 5},
        {**STATS, "tasks": 9, "asr_calls": 2, "mt_cache_hits": 3},
        {"type": "error", "code": "mic_silent", "message": "silent"},
        {"type": "error", "code": "mic_silent", "message": "silent"},
        {"type": "session", "state": "ended"},
    ])

    asyncio.run(drain_teacher(ws, results))

    assert results.partials == {5: ["こん", "こんにちは"]}
    assert results.finals == {5: "こんにちは", 0: "次"}
    assert results.published_seqs == {1, 2}
    assert results.first_partial_at is not None
    assert results.first_final_at is not None
    assert results.partial_at[5] <= results.final_at[5]
    assert results.stats_samples[0]["asr_calls"] == 2
    assert results.stats_samples[0]["mt_cache_hits"] == 3
    assert results.tasks == [9.0]
    assert results.notices == {"mic_silent": 2}
    assert results.errors == []


def test_teacher_keeps_legacy_stats_defaults_and_report_shape() -> None:
    results = Results()

    asyncio.run(drain_teacher(Socket([json.dumps(STATS).encode()]), results))

    sample = results.stats_samples[0]
    expected = {
        "queue_depth": 2,
        "median_delay_ms": 123,
        "audio_queue_seconds": 0.0,
        "mt_queue_depth": 0,
        "overloaded": False,
        "mt_cache_hit_rate": 0.0,
        "mt_cache_hits": 0,
        "mt_cache_size": 0,
        "asr_calls": 0,
        "asr_partial_calls": 0,
        "mt_calls": 0,
        "audio_frames_rejected": 0,
        "asr_dropped_segments": 0,
        "asr_dropped_seconds": 0.0,
        "tasks": 0,
    }
    assert {key: sample[key] for key in expected} == expected
    assert results.tasks == []
    assert results.dropped_audio().segments == 0


INVALID_MESSAGES = [
    "not json",
    b"{",
    {"type": "unexpected"},
    {"type": "caption", "seq": 1},
    {"type": "error", "code": "unexpected", "message": "bad"},
]


@pytest.mark.parametrize("invalid", INVALID_MESSAGES)
def test_teacher_reports_invalid_stream_message(invalid) -> None:
    results = Results()

    asyncio.run(drain_teacher(Socket([invalid]), results))

    assert len(results.errors) == 1
    assert results.errors[0].startswith("teacher protocol error: invalid server message:")
    assert results.captions == {}


def test_teacher_waits_for_joined_before_starting(monkeypatch) -> None:
    ws = Socket([STATS, {"type": "speaking", "on": False}, JOINED])
    connect_sockets(monkeypatch, ws)
    results = Results()

    asyncio.run(run_teacher(8100, "1234", np.array([], dtype=np.int16), results))

    assert ws.sent[1] == ({"type": "control", "action": "start"}, 3)
    assert ws.sent[2][0] == {"type": "control", "action": "end"}
    assert len(results.stats_samples) == 1
    assert results.errors == []


@pytest.mark.parametrize("invalid", INVALID_MESSAGES)
def test_teacher_reports_invalid_join_response_without_sending_audio(monkeypatch, invalid) -> None:
    ws = Socket([invalid])
    connect_sockets(monkeypatch, ws)
    results = Results()

    asyncio.run(run_teacher(8100, "1234", np.zeros(1600, dtype=np.int16), results))

    assert len(ws.sent) == 1
    assert results.audio_started_at is None
    assert results.errors[0].startswith("teacher protocol error: invalid server message:")


def test_teacher_reports_join_rejection_without_starting(monkeypatch) -> None:
    ws = Socket([{"type": "join_rejected", "reason": "bad_code"}])
    connect_sockets(monkeypatch, ws)
    results = Results()

    asyncio.run(run_teacher(8100, "1234", np.zeros(1600, dtype=np.int16), results))

    assert len(ws.sent) == 1
    assert results.errors == ["teacher: join_rejected bad_code"]


@pytest.mark.parametrize("invalid", INVALID_MESSAGES)
def test_student_reports_invalid_message_without_retrying(monkeypatch, invalid) -> None:
    connected = connect_sockets(monkeypatch, Socket([JOINED, invalid]))
    results = Results()

    asyncio.run(run_student(8100, "1234", "en", results, "s1"))

    assert len(connected) == 1
    assert len(results.errors) == 1
    assert results.errors[0].startswith("student en protocol error: invalid server message:")
    assert results.live_ws == {}


def test_student_records_captions_and_rejoins_with_highest_seq(monkeypatch) -> None:
    first = Socket([JOINED, caption(4), caption(2)])
    second = Socket([
        {**JOINED, "seq_head": 4},
        caption(4, delay_ms=0),
        {"type": "join_rejected", "reason": "full"},
    ])
    connected = connect_sockets(monkeypatch, first, second)

    async def no_backoff(seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_backoff)
    results = Results()

    asyncio.run(run_student(8100, "1234", "en", results, "s1"))

    assert len(connected) == 2
    assert "last_seq" not in first.sent[0][0]
    assert second.sent[0][0]["last_seq"] == 4
    assert results.student_seqs == {"s1": {2, 4}}
    assert results.student_langs == {"s1": "en"}
    assert results.captions == {(4, "en"): 123, (2, "en"): 123}
    assert results.first_caption_at is not None
    assert results.disconnects == 1
    assert results.errors == ["student en: join_rejected full"]
    assert results.live_ws == {}


@pytest.mark.parametrize("reason", ["bad_code", "bad_lang", "rate_limited", "full"])
def test_student_records_join_rejection_without_retrying(monkeypatch, reason) -> None:
    connected = connect_sockets(monkeypatch, Socket([{"type": "join_rejected", "reason": reason}]))
    results = Results()

    asyncio.run(run_student(8100, "1234", "en", results, "s1"))

    assert len(connected) == 1
    assert results.errors == [f"student en: join_rejected {reason}"]
    assert results.disconnects == 0
    assert results.reconnect_failures == 0
    assert results.live_ws == {}


def test_record_caption_still_accepts_existing_dict_callers() -> None:
    results = Results()

    results.record_caption({"seq": 1, "lang": "en", "delay_ms": 12})

    assert results.captions == {(1, "en"): 12}
