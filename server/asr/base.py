"""ASREngine 抽象（plan.md §6.5）。テストと実装の合意済みシーム。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ASRResult:
    text: str
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    compression_ratio: float = 1.0  # 幻覚フィルタ（E-04, #11）で使用


class ASREngine(ABC):
    @abstractmethod
    def transcribe(self, pcm16: np.ndarray, sample_rate: int) -> ASRResult:
        """発話1件分のPCM16（int16 mono）を文字起こしする。ワーカースレッドで呼ばれる。"""

    def warmup(self) -> None:
        """起動時ロード＆ダミー推論（初回遅延対策）。フェイクでは何もしない。"""
