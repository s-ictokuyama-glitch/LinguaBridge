"""WebSocketメッセージのスキーマ（plan.md §6.2 の契約）。

テキストフレーム=JSON、バイナリフレーム=16kHz mono PCM16（スキーマ対象外）。
クライアント→サーバーは parse_client_message で検証し、
サーバー→クライアントは各モデルの model_dump() を send_json する。
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from server.config import Language


class ProtocolError(Exception):
    pass


SessionState = Literal["idle", "live", "paused", "ended"]
JoinRejectReason = Literal["bad_code", "bad_lang"]


# ---- クライアント → サーバー ----


class JoinMessage(BaseModel):
    type: Literal["join"]
    role: Literal["teacher", "student"]
    code: str
    lang: str | None = None  # 生徒のみ
    last_seq: int | None = None  # 再接続時のみ（F-11）


class SetLangMessage(BaseModel):
    type: Literal["set_lang"]
    lang: str


class ControlMessage(BaseModel):
    type: Literal["control"]
    action: Literal["start", "pause", "end"]


class RecordingMessage(BaseModel):
    type: Literal["recording"]
    on: bool


ClientMessage = Union[JoinMessage, SetLangMessage, ControlMessage, RecordingMessage]

_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(
    Annotated[ClientMessage, Field(discriminator="type")]
)


def parse_client_message(raw: str | bytes) -> ClientMessage:
    try:
        return _client_adapter.validate_json(raw)
    except ValidationError as exc:
        raise ProtocolError(f"invalid client message: {exc.error_count()} error(s)") from exc


# ---- サーバー → クライアント ----


class Joined(BaseModel):
    type: Literal["joined"] = "joined"
    seq_head: int
    languages: list[Language]
    session_state: SessionState


class JoinRejected(BaseModel):
    type: Literal["join_rejected"] = "join_rejected"
    reason: JoinRejectReason


class Caption(BaseModel):
    type: Literal["caption"] = "caption"
    seq: int
    ja: str
    text: str
    lang: str
    delay_ms: int


class SessionStateMsg(BaseModel):
    type: Literal["session"] = "session"
    state: SessionState


class AsrFinal(BaseModel):
    type: Literal["asr_final"] = "asr_final"
    seq: int
    ja: str
    asr_ms: int


class Stats(BaseModel):
    # 定期配信の実装は #15（先生モニタリング）。契約のみここで確立する
    type: Literal["stats"] = "stats"
    students: int
    langs: dict[str, int]
    queue_depth: int
    median_delay_ms: int


class ErrorMsg(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
