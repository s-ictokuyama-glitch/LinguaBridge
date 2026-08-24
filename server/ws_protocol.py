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
# "full" は生徒の同時接続上限に達したとき（#25 A-4 / limits.max_students）
JoinRejectReason = Literal["bad_code", "bad_lang", "rate_limited", "full"]


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
    # 履歴で復元可能な最古のseq。これより前は再送されない（履歴上限で切れた分）。
    # クライアントは last_seq の下限をこの値-1に引き上げて恒久欠落を確定させる
    history_from: int
    languages: list[Language]
    session_state: SessionState
    recording: bool = False  # 参加時点の記録状態（記録中インジケーター表示用 F-10）
    # 参加時点で先生が発話中か（#29）。発話の途中で参加した生徒にも
    # インジケーターが正しく出るようにする。既定値つき＝既存クライアントは無視できる
    speaking: bool = False


class RecordingState(BaseModel):
    # 記録ON/OFFの切替を全クライアントへ通知（先生・生徒双方のインジケーター用 F-10）
    type: Literal["recording"] = "recording"
    on: bool


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
    # この発話の turn_id（#29）。先生UIは同じ turn_id の partial 行をこれで差し替える。
    # 既定値つき＝ partial を使わないクライアントは無視できる
    turn_id: int = 0


class TurnPartial(BaseModel):
    """確定前の暫定テキスト（#29）。**先生にだけ**送る。

    生徒には送らない。翻訳は final のみという方針上、生徒に見せられるのは未翻訳の
    日本語になり、英語/中国語を選んだ生徒には無意味なため（チケットの決定事項）。

    不変条件: 履歴に載らない・記録に載らない・翻訳へ流れない。
    同じ turn_id の中で revision が単調増加し、後から来た revision が前を上書きする。
    """

    type: Literal["turn.partial"] = "turn.partial"
    turn_id: int
    revision: int
    ja: str


class Speaking(BaseModel):
    """先生が発話中か（#29）。全クライアントへ送る。

    生徒は「先生が話しています」インジケーターに使い（日本語の原文は1文字も見せない）、
    先生は partial 行を消す契機に使う。**VAD 由来**なので ASR の完了を待たない
    ——詰まっているときこそ「話しているのに字幕が出ない」ことが伝わる必要がある。

    状態が変化したときだけ送る（毎フレーム送らない）。
    """

    type: Literal["speaking"] = "speaking"
    on: bool


class Stats(BaseModel):
    type: Literal["stats"] = "stats"
    students: int
    langs: dict[str, int]
    queue_depth: int
    median_delay_ms: int
    overloaded: bool = False  # キュー滞留による過負荷（E-05）。解消で False に戻る
    # ASR待ち＋処理中の音声の長さ（秒）。queue_depth（件数）と違い「どれだけ遅れているか」を
    # 実時間で表す。ベースライン計測（#24）の計装で、既定値つき＝既存クライアントは無視できる
    audio_queue_seconds: float = 0.0
    # 遅延の内訳（#30）。`audio_queue_seconds` は「待ち」と「処理中」の合計で、
    # 分けずに先生へ出すと「1発話が長いだけ」を「詰まっている」と誤読させる
    # （#25・#29 で二度確認された読み間違い）。合計の意味は変えずに内訳を足す:
    #   asr_wait_seconds + asr_active_seconds == audio_queue_seconds
    asr_wait_seconds: float = 0.0  # まだ ASR に入っていない、キューで待っている音声
    asr_active_seconds: float = 0.0  # いま ASR が処理しているセグメントの長さ
    median_asr_ms: int = 0  # 確定 Segment 1件あたりの ASR 推論時間（partial は含めない）
    mt_queue_depth: int = 0  # 翻訳待ちの件数（queue_depth は ASR と合算していて分からない）
    median_mt_ms: int = 0  # **実際に推論した**翻訳1件あたりの時間（キャッシュヒットは除く）
    # 発話をまたぐ翻訳キャッシュ（#26 B-4）。hit_rate は 0..1（参照0回なら 0.0）。
    # いずれも既定値つき＝既存クライアントは無視できる
    mt_cache_hit_rate: float = 0.0
    mt_cache_hits: int = 0
    mt_cache_size: int = 0


class ErrorMsg(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
