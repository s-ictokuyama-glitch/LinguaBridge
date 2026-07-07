"""単一ルームのセッション状態（plan.md §5）。

すべてインメモリ・揮発。WS送信は行わず（pipeline / main 側の責務）、
同期的な状態管理だけを持つのでユニットテストが素直に書ける。
"""

from __future__ import annotations

import secrets
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Protocol

from server.ws_protocol import SessionState

if TYPE_CHECKING:
    from server.pipeline import Utterance


class ClientSocket(Protocol):
    """クライアントへの送信口。starlette WebSocket が構造的に満たす。"""

    async def send_json(self, data: Any) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


def generate_join_code() -> str:
    return f"{secrets.randbelow(10000):04d}"


@dataclass
class Client:
    id: str
    role: Literal["teacher", "student"]
    lang: str | None  # 生徒のみ
    ws: ClientSocket | None  # ユニットテストでは None
    joined_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class Session:
    def __init__(self, join_code: str, history_len: int = 50) -> None:
        self.join_code = join_code
        self.started_at = datetime.now(timezone.utc)
        self.state: SessionState = "idle"
        self.auto_paused = False  # 先生切断による自動一時停止か（再接続で自動再開する）
        self.recording = False  # F-10。書き出しは #18
        self.clients: dict[str, Client] = {}
        self.history: deque[Utterance] = deque(maxlen=history_len)
        self._seq = 0

    @property
    def seq_head(self) -> int:
        return self._seq

    @property
    def history_from(self) -> int:
        """履歴で復元可能な最古のseq（履歴が空なら次に発行されるseq）。"""
        return self.history[0].seq if self.history else self._seq + 1

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def check_code(self, code: str) -> bool:
        return code == self.join_code

    def add_client(self, client: Client) -> None:
        self.clients[client.id] = client

    def remove_client(self, client_id: str) -> None:
        self.clients.pop(client_id, None)

    def students(self) -> list[Client]:
        return [c for c in self.clients.values() if c.role == "student"]

    def teacher(self) -> Client | None:
        for c in self.clients.values():
            if c.role == "teacher":
                return c
        return None

    def active_langs(self) -> set[str]:
        """選択中の生徒が1人以上いる言語。翻訳ジョブはこの言語にだけ発生する。"""
        return {c.lang for c in self.students() if c.lang}

    def add_history(self, utterance: Utterance) -> None:
        self.history.append(utterance)

    def history_entries_since(self, last_seq: int) -> list[Utterance]:
        """再接続時の差分再送（F-11）: last_seq より後の発話（訳文の有無を問わない。
        訳文が無いものは呼び出し側がオンデマンド翻訳を依頼する）。"""
        return [utt for utt in self.history if utt.seq > last_seq]
