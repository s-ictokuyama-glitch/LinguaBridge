"""決定的フェイクASR（#10 トレーサー用）。

既知のPCMパターン → 既知の日本語文:
    発話中の最初の非ゼロサンプル値が PHRASES のキーに一致すればその文を返す。
    テストは「振幅 k の定数波形を送る → PHRASES[k] が字幕になる」ことを検証できる。

それ以外（実マイク音声など）は発話長に応じた汎用文を返すので、
実機デモでも「話すと生徒画面にカードが出る」ことを確認できる。
"""

from __future__ import annotations

import numpy as np

from server.asr.base import ASREngine, ASRResult


class FakeASREngine(ASREngine):
    PHRASES: dict[int, str] = {
        1000: "おはようございます。",
        2000: "光合成には日光が必要です。",
        3000: "今日は天気がいいですね。",
    }

    def __init__(self) -> None:
        self.calls: list[ASRResult] = []  # テストからの検査用

    def transcribe(self, pcm16: np.ndarray, sample_rate: int) -> ASRResult:
        nonzero = pcm16[pcm16 != 0]
        if nonzero.size == 0:
            result = ASRResult(text="", no_speech_prob=1.0)
        else:
            key = int(nonzero[0])
            text = self.PHRASES.get(
                key, f"（フェイク認識: {pcm16.size / sample_rate:.1f}秒の発話）"
            )
            result = ASRResult(text=text)
        self.calls.append(result)
        return result
