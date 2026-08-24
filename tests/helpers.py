"""テスト用のPCM生成ヘルパー。

FakeASREngine の規約: 発話中の最初の非ゼロサンプル値がフレーズ辞書のキー
（1000/2000/3000）に一致すると既知の日本語文になる。
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np

SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # ブラウザが送る100msフレーム相当


def speech_pcm(key: int, seconds: float) -> np.ndarray:
    return np.full(int(SAMPLE_RATE * seconds), key, dtype=np.int16)


def silence_pcm(seconds: float) -> np.ndarray:
    return np.zeros(int(SAMPLE_RATE * seconds), dtype=np.int16)


def chunks(pcm: np.ndarray, chunk_bytes: int = CHUNK_BYTES) -> Iterator[bytes]:
    data = pcm.tobytes()
    for i in range(0, len(data), chunk_bytes):
        yield data[i : i + chunk_bytes]


def utterance_bytes(key: int, speech_s: float = 1.0, silence_s: float = 1.2) -> Iterator[bytes]:
    """発話1件分（音声＋発話終了判定に足る無音）を100msチャンク列で返す。

    無音は既定 turn 戦略（morph）の `force_silence_ms`(1000ms) を超える長さにしてある。
    PHRASES[3000]「今日は天気がいいですね。」は文法上「継続」に分類されるので、
    無音がここに届かないと Turn が確定せず字幕が出ない（#27）。
    """
    pcm = np.concatenate([speech_pcm(key, speech_s), silence_pcm(silence_s)])
    return chunks(pcm)
