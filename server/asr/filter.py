"""Whisper幻覚フィルタ（plan.md E-04）。

無音・雑音区間で Whisper が生成しがちな定型文を、配信前に多重チェックで破棄する:
no_speech_prob / compression_ratio / avg_logprob / 既知幻覚フレーズ辞書。
方針は「授業内容を落とさない」（E-05）とのバランスで、閾値は Whisper 本家の
既定値に合わせて保守的にする。
"""

from __future__ import annotations

import re

from server.asr.base import ASRResult

NO_SPEECH_PROB_LIMIT = 0.6
COMPRESSION_RATIO_LIMIT = 2.4  # 反復生成（同語連発）の検出
AVG_LOGPROB_LIMIT = -1.0  # Whisper の logprob_threshold と同値

# 無音・雑音でWhisper系モデルが生成しやすい定型フレーズ（正規化後の完全一致）
KNOWN_HALLUCINATIONS = frozenset(
    {
        "ご視聴ありがとうございました",
        "ご清聴ありがとうございました",
        "チャンネル登録お願いします",
        "チャンネル登録よろしくお願いします",
        "最後までご視聴いただきありがとうございました",
        "おやすみなさい",
        "字幕は自動生成です",
        "thankyouforwatching",
    }
)

_STRIP_PATTERN = re.compile(r"[\s。、．，!！?？・…〜~\-\.]")


def _normalize(text: str) -> str:
    return _STRIP_PATTERN.sub("", text).lower()


def hallucination_reason(result: ASRResult) -> str | None:
    """破棄すべきなら理由文字列を、配信してよいなら None を返す。"""
    text = result.text.strip()
    if not text:
        return "empty"
    if result.no_speech_prob > NO_SPEECH_PROB_LIMIT:
        return f"no_speech_prob={result.no_speech_prob:.2f}"
    if result.compression_ratio > COMPRESSION_RATIO_LIMIT:
        return f"compression_ratio={result.compression_ratio:.2f}"
    if result.avg_logprob < AVG_LOGPROB_LIMIT:
        return f"avg_logprob={result.avg_logprob:.2f}"
    if _normalize(text) in KNOWN_HALLUCINATIONS:
        return "known_phrase"
    return None
