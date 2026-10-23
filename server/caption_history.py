"""確定字幕の採番と再接続用の履歴。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class Translation:
    lang: str
    text: str
    engine: str
    mt_ms: int


@dataclass
class Utterance:
    """確定した Turn（字幕カード1枚）。履歴・記録・翻訳ジョブの単位。"""

    seq: int
    t_start: float
    t_end: float
    text_ja: str
    asr_ms: int  # 構成 Segment の合計（morph では複数回ぶん）
    created_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    translations: dict[str, Translation] = field(default_factory=dict)
    segments: int = 1  # 連結した Segment 数（1 なら連結なし）


class CaptionHistory:
    def __init__(self, maxlen: int = 50) -> None:
        self._entries: deque[Utterance] = deque(maxlen=maxlen)
        self._seq = 0

    @property
    def seq_head(self) -> int:
        return self._seq

    @property
    def history_from(self) -> int:
        return self._entries[0].seq if self._entries else self._seq + 1

    def append(self, *, t_start: float, t_end: float, text_ja: str,
               asr_ms: int, segments: int = 1) -> Utterance:
        self._seq += 1
        utterance = Utterance(self._seq, t_start, t_end, text_ja, asr_ms, segments=segments)
        self._entries.append(utterance)
        return utterance

    def since(self, last_seq: int) -> list[Utterance]:
        return [entry for entry in self._entries if entry.seq > last_seq]
