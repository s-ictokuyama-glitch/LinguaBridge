"""先生WSから届くバイナリフレーム（16kHz mono PCM16）の受け口。"""

from __future__ import annotations

import numpy as np


def pcm16_from_bytes(data: bytes) -> np.ndarray:
    if len(data) % 2:
        data = data[:-1]  # 奇数長は末尾バイト破損とみなし切り捨て
    return np.frombuffer(data, dtype=np.int16)
