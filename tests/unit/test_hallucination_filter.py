"""幻覚フィルタ（plan.md E-04）のユニットテスト。"""

from __future__ import annotations

import pytest

from server.asr.base import ASRResult
from server.asr.hallucination import hallucination_reason


def ok_result(text: str = "光合成には日光が必要です。") -> ASRResult:
    return ASRResult(text=text, avg_logprob=-0.3, no_speech_prob=0.05, compression_ratio=1.2)


def test_normal_speech_passes():
    assert hallucination_reason(ok_result()) is None


def test_empty_text_dropped():
    assert hallucination_reason(ASRResult(text="   ")) == "empty"


def test_high_no_speech_prob_dropped():
    result = ASRResult(text="ご視聴", no_speech_prob=0.9)
    assert "no_speech_prob" in (hallucination_reason(result) or "")


def test_high_compression_ratio_dropped():
    result = ASRResult(text="あああああああああ", compression_ratio=3.5, avg_logprob=-0.3)
    assert "compression_ratio" in (hallucination_reason(result) or "")


def test_low_avg_logprob_dropped():
    result = ASRResult(text="うにゃむにゃ", avg_logprob=-1.8)
    assert "avg_logprob" in (hallucination_reason(result) or "")


@pytest.mark.parametrize(
    "text",
    [
        "ご視聴ありがとうございました",
        "ご視聴ありがとうございました。",
        " ご視聴、ありがとうございました！ ",
        "チャンネル登録お願いします",
    ],
)
def test_known_hallucination_phrases_dropped(text: str):
    # 信頼度メトリクスが正常でも既知フレーズは破棄する
    assert hallucination_reason(ok_result(text)) == "known_phrase"


def test_phrase_inside_longer_speech_passes():
    # 実際の授業発話に含まれる場合（完全一致でない）は落とさない
    text = "今日の授業はここまでです。ご視聴ありがとうございましたと言うのはテレビの話です。"
    assert hallucination_reason(ok_result(text)) is None
