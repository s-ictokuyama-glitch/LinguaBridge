"""VoiceSegmenter の発話セグメンテーション（無音500ms終了・30s強制分割）のユニットテスト。"""

from __future__ import annotations

import numpy as np
import pytest

from server.audio.vad import EnergyVAD, VoiceSegmenter
from tests.helpers import SAMPLE_RATE, chunks, silence_pcm, speech_pcm


def make_segmenter(min_silence_ms: int = 500, max_utterance_s: int = 30) -> VoiceSegmenter:
    return VoiceSegmenter(
        EnergyVAD(threshold=300),
        sample_rate=SAMPLE_RATE,
        min_silence_ms=min_silence_ms,
        max_utterance_s=max_utterance_s,
    )


def feed_all(seg: VoiceSegmenter, pcm: np.ndarray) -> list:
    out = []
    for chunk in chunks(pcm):
        out.extend(seg.feed(np.frombuffer(chunk, dtype=np.int16)))
    return out


def test_speech_followed_by_silence_yields_one_segment():
    seg = make_segmenter()
    pcm = np.concatenate([speech_pcm(2000, 1.0), silence_pcm(0.8)])
    segments = feed_all(seg, pcm)
    assert len(segments) == 1
    s = segments[0]
    assert s.t_start == pytest.approx(0.0, abs=0.15)
    assert s.t_end == pytest.approx(1.0, abs=0.15)
    # FakeASR の規約が成立するよう、先頭の非ゼロサンプルはパターン値
    nonzero = s.pcm[s.pcm != 0]
    assert nonzero.size > 0 and int(nonzero[0]) == 2000
    assert not s.forced


def test_silence_only_yields_nothing():
    seg = make_segmenter()
    assert feed_all(seg, silence_pcm(3.0)) == []
    assert seg.flush() is None


def test_short_pause_does_not_split():
    seg = make_segmenter(min_silence_ms=500)
    pcm = np.concatenate(
        [speech_pcm(1000, 0.5), silence_pcm(0.3), speech_pcm(1000, 0.5), silence_pcm(0.8)]
    )
    segments = feed_all(seg, pcm)
    assert len(segments) == 1


def test_two_utterances_split_by_long_silence():
    seg = make_segmenter()
    pcm = np.concatenate(
        [speech_pcm(1000, 0.5), silence_pcm(0.8), speech_pcm(2000, 0.5), silence_pcm(0.8)]
    )
    segments = feed_all(seg, pcm)
    assert len(segments) == 2
    assert int(segments[0].pcm[segments[0].pcm != 0][0]) == 1000
    assert int(segments[1].pcm[segments[1].pcm != 0][0]) == 2000
    # 2発話目の開始は1発話目の終了より後
    assert segments[1].t_start > segments[0].t_end


def test_forced_split_at_max_utterance():
    seg = make_segmenter(max_utterance_s=2)
    segments = feed_all(seg, speech_pcm(1000, 5.0))
    # 2秒で強制分割が起き、話し続けている間に少なくとも2回発火する
    assert len(segments) >= 2
    assert all(s.forced for s in segments)
    assert segments[0].pcm.size <= 2 * SAMPLE_RATE + SAMPLE_RATE // 10


def test_flush_returns_open_utterance():
    seg = make_segmenter()
    assert feed_all(seg, speech_pcm(3000, 1.0)) == []  # 無音が来ていないので未確定
    flushed = seg.flush()
    assert flushed is not None
    assert int(flushed.pcm[flushed.pcm != 0][0]) == 3000
    assert seg.flush() is None  # 二重フラッシュは空


def test_arbitrary_chunk_sizes_equivalent():
    pcm = np.concatenate([speech_pcm(2000, 1.0), silence_pcm(0.8)])
    seg = make_segmenter()
    segments = []
    data = pcm.tobytes()
    # 100msの倍数でない不揃いなチャンクで送る
    for i in range(0, len(data), 1234 * 2):
        segments.extend(seg.feed(np.frombuffer(data[i : i + 1234 * 2], dtype=np.int16)))
    segments.extend(seg.feed(np.zeros(0, dtype=np.int16)))
    flushed = seg.flush()
    total = segments + ([flushed] if flushed else [])
    assert len(total) == 1
