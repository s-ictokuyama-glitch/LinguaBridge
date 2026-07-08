"""決定的フェイクASR（#10 トレーサー用）。

既知のPCMパターン → 既知の日本語文:
    発話中の最初の非ゼロサンプル値が PHRASES のキーに一致すればその文を返す。
    テストは「振幅 k の定数波形を送る → PHRASES[k] が字幕になる」ことを検証できる。
    振幅 4000 は既知の幻覚フレーズを返す（幻覚フィルタの配信経路テスト用）。

それ以外（実マイク音声など）は発話長に応じた汎用文を返すので、
実機デモでも「話すと生徒画面にカードが出る」ことを確認できる。

warmup_gate（threading.Event）を渡すと warmup がそれをセットされるまでブロックする。
モデルロード中の /healthz 503 を決定的に検証するテスト用。
"""

from __future__ import annotations

import threading

import numpy as np

from server.asr.base import ASREngine, ASRResult


class FakeASREngine(ASREngine):
    PHRASES: dict[int, str] = {
        1000: "おはようございます。",
        2000: "光合成には日光が必要です。",
        3000: "今日は天気がいいですね。",
        4000: "ご視聴ありがとうございました",  # 既知幻覚フレーズ（E-04テスト用）
    }

    def __init__(self, warmup_gate: threading.Event | None = None) -> None:
        self.calls: list[ASRResult] = []  # テストからの検査用
        self._warmup_gate = warmup_gate

    def warmup(self) -> None:
        if self._warmup_gate is not None:
            self._warmup_gate.wait()  # gate がセットされるまでロード完了扱いにしない

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
