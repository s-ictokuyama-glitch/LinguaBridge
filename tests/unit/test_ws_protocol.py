"""WSメッセージスキーマ（plan.md §6.2 の契約）のバリデーションテスト。"""

from __future__ import annotations

import pytest

from server.ws_protocol import (
    Caption,
    ControlMessage,
    Joined,
    JoinMessage,
    JoinRejected,
    ProtocolError,
    RecordingMessage,
    SetLangMessage,
    parse_client_message,
)


def test_parse_student_join():
    msg = parse_client_message(
        '{"type": "join", "role": "student", "code": "4831", "lang": "zh", "last_seq": 12}'
    )
    assert isinstance(msg, JoinMessage)
    assert msg.role == "student"
    assert msg.code == "4831"
    assert msg.lang == "zh"
    assert msg.last_seq == 12


def test_parse_teacher_join_without_lang():
    msg = parse_client_message('{"type": "join", "role": "teacher", "code": "4831"}')
    assert isinstance(msg, JoinMessage)
    assert msg.lang is None
    assert msg.last_seq is None


def test_parse_set_lang():
    msg = parse_client_message('{"type": "set_lang", "lang": "en"}')
    assert isinstance(msg, SetLangMessage)
    assert msg.lang == "en"


def test_parse_control():
    msg = parse_client_message('{"type": "control", "action": "pause"}')
    assert isinstance(msg, ControlMessage)
    assert msg.action == "pause"


def test_parse_recording():
    msg = parse_client_message('{"type": "recording", "on": true}')
    assert isinstance(msg, RecordingMessage)
    assert msg.on is True


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"type": "unknown"}',
        '{"type": "join", "role": "admin", "code": "1"}',  # 不正role
        '{"type": "control", "action": "explode"}',  # 不正action
        '{"type": "set_lang"}',  # lang欠落
        '{"type": "join", "role": "student"}',  # code欠落
    ],
)
def test_invalid_messages_raise(raw: str):
    with pytest.raises(ProtocolError):
        parse_client_message(raw)


def test_server_messages_serialize_with_type_tag():
    assert Joined(seq_head=0, languages=[], session_state="idle").model_dump()["type"] == "joined"
    cap = Caption(seq=1, ja="こんにちは", text="[en] こんにちは", lang="en", delay_ms=42)
    assert cap.model_dump() == {
        "type": "caption",
        "seq": 1,
        "ja": "こんにちは",
        "text": "[en] こんにちは",
        "lang": "en",
        "delay_ms": 42,
    }
    assert JoinRejected(reason="bad_code").model_dump()["type"] == "join_rejected"
