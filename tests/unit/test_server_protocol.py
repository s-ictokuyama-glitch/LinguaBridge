from __future__ import annotations

import asyncio
import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import get_args

import pytest
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient

from server import ws_protocol as proto
from tests.helpers import RecordingSocket, wait_for

MESSAGES = [
    proto.Joined(seq_head=0, history_from=1, languages=[], session_state="idle"),
    proto.RecordingState(on=True),
    proto.JoinRejected(reason="bad_code"),
    proto.Caption(seq=1, ja="こんにちは", text="Hello", lang="en", delay_ms=42),
    proto.SessionStateMsg(state="live"),
    proto.AsrFinal(seq=1, ja="こんにちは", asr_ms=12),
    proto.TurnPartial(turn_id=1, revision=2, ja="こんにちは"),
    proto.Speaking(on=True),
    proto.Stats(students=1, langs={"en": 1}, queue_depth=0, median_delay_ms=42),
    proto.ErrorMsg(code="bad_message", message="invalid message"),
]


@pytest.fixture(params=MESSAGES, ids=lambda message: message.type)
def message(request) -> proto.ServerMessage:
    return request.param


def test_samples_cover_every_server_message_model():
    assert {type(message) for message in MESSAGES} == set(get_args(proto.ServerMessage))


@pytest.mark.parametrize("as_bytes", [False, True], ids=["str", "bytes"])
def test_server_message_round_trip(message, as_bytes):
    raw = message.model_dump_json()
    parsed = proto.parse_server_message(raw.encode() if as_bytes else raw)
    assert type(parsed) is type(message)
    assert parsed.model_dump() == message.model_dump()


@pytest.mark.parametrize(
    "raw",
    ["not json", "null", "[]", "{}", '{"type":"unknown"}', '{"type":"error","code":"unknown","message":"x"}'],
)
def test_invalid_server_messages_raise_protocol_error(raw):
    with pytest.raises(proto.ProtocolError, match="invalid server message"):
        proto.parse_server_message(raw)


def test_each_required_server_field_is_validated(message):
    for name, field in type(message).model_fields.items():
        if field.is_required():
            payload = message.model_dump()
            del payload[name]
            with pytest.raises(proto.ProtocolError, match="invalid server message"):
                proto.parse_server_message(json.dumps(payload))


@pytest.mark.parametrize("code", get_args(proto.ErrorCode))
def test_actual_error_codes_round_trip(code):
    message = proto.ErrorMsg(code=code, message="warning")
    assert proto.parse_server_message(message.model_dump_json()) == message


def test_legacy_stats_payload_keeps_all_defaults():
    payload = {"type": "stats", "students": 1, "langs": {"en": 1}, "queue_depth": 0, "median_delay_ms": 42}
    message = proto.parse_server_message(json.dumps(payload))
    assert isinstance(message, proto.Stats)
    assert message.model_dump() == {
        **payload,
        "overloaded": False,
        "audio_queue_seconds": 0.0,
        "asr_wait_seconds": 0.0,
        "asr_active_seconds": 0.0,
        "median_asr_ms": 0,
        "mt_queue_depth": 0,
        "median_mt_ms": 0,
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


@contextmanager
def websocket_frames(frames):
    async def stream(socket):
        await socket.accept()
        for frame in frames:
            await socket.send_text(frame)
        await socket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", stream)])
    with TestClient(app) as client, client.websocket_connect("/ws") as socket:
        yield socket


def test_sync_helper_handles_every_server_model(message):
    with websocket_frames([message.model_dump_json()]) as socket:
        result = wait_for(socket, type(message))
    assert type(result) is type(message)
    assert result == message


def test_sync_helper_filters_fields_and_records_all_frames():
    frames = [proto.Speaking(on=True), proto.SessionStateMsg(state="live"), proto.SessionStateMsg(state="paused")]
    received: list[proto.ServerMessage] = []
    with websocket_frames(frame.model_dump_json() for frame in frames) as socket:
        result = wait_for(socket, proto.SessionStateMsg, state="paused", received=received)
    assert result.state == "paused"
    assert received == frames


@pytest.mark.parametrize("raw", ['{"type":"unknown"}', '{"type":"caption"}'])
def test_sync_helper_rejects_invalid_frames_before_target(raw):
    with websocket_frames([raw, proto.Speaking(on=True).model_dump_json()]) as socket:
        with pytest.raises(proto.ProtocolError):
            wait_for(socket, proto.Speaking)


def test_async_helper_preserves_dict_frames_and_arbitrary_tags():
    async def check():
        socket = RecordingSocket()
        frames = [{"type": "speaking", "on": True}, {"type": "barrier", "seq": 1}, {"type": "barrier", "seq": 2}]
        for frame in frames:
            await socket.send_json(frame)
        result = await wait_for(socket, "barrier", seq=2)
        assert result is frames[-1]
        assert socket.sent == frames

    asyncio.run(check())


WEB = Path(__file__).resolve().parents[2] / "web"
IGNORED_TYPES = {
    "student": {
        "asr_final": "日本語の確定文字起こしは先生専用。",
        "turn.partial": "日本語の暫定文字起こしは先生専用。",
        "stats": "処理状況は先生専用。",
    },
    "teacher": {"caption": "翻訳字幕は生徒へ配送する。"},
}


def assert_browser_contract(source, ignored):
    switch = re.search(r"switch\s*\(\s*msg\.type\s*\)\s*\{(.*?)^  \}", source, re.MULTILINE | re.DOTALL)
    assert switch is not None, "msg.type switch not found"
    handled = set(re.findall(r'case\s+["\']([^"\']+)["\']\s*:', switch.group(1)))
    declared = {model.model_fields["type"].default for model in get_args(proto.ServerMessage)}
    assert handled.isdisjoint(ignored), "handled type must not be marked ignored"
    assert handled | set(ignored) == declared


@pytest.mark.parametrize("role", IGNORED_TYPES)
def test_browser_message_types_match_server_union_in_both_directions(role):
    source = (WEB / f"{role}.js").read_text(encoding="utf-8")
    assert_browser_contract(source, IGNORED_TYPES[role])


@pytest.mark.parametrize("role", IGNORED_TYPES)
@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_browser_contract_detects_missing_and_extra_cases(role, mutation):
    source = (WEB / f"{role}.js").read_text(encoding="utf-8")
    if mutation == "missing":
        source = source.replace('case "joined":', "", 1)
    else:
        source = source.replace('case "joined":', 'case "unrecognized":\n    case "joined":', 1)
    with pytest.raises(AssertionError):
        assert_browser_contract(source, IGNORED_TYPES[role])


@pytest.mark.parametrize("role", IGNORED_TYPES)
def test_browser_special_error_codes_are_declared(role):
    source = (WEB / f"{role}.js").read_text(encoding="utf-8")
    codes = set(re.findall(r'msg\.code\s*===\s*["\']([^"\']+)["\']', source))
    assert codes
    assert codes <= set(get_args(proto.ErrorCode))
