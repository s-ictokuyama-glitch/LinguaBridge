"""VoiceSegmenter の発話セグメンテーション（無音500ms終了・30s強制分割）のユニットテスト。"""

from __future__ import annotations

import numpy as np
import pytest

from server.audio.vad import EnergyVAD, Segment, VoiceSegmenter
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


def test_pre_roll_included_before_speech_onset():
    # 発話開始判定より前の音声（プリロール）がセグメント先頭に含まれる（語頭の欠け防止）
    seg = VoiceSegmenter(
        EnergyVAD(threshold=300),
        sample_rate=SAMPLE_RATE,
        min_silence_ms=500,
        max_utterance_s=30,
        pre_roll_ms=200,
    )
    pcm = np.concatenate([silence_pcm(1.0), speech_pcm(2000, 1.0), silence_pcm(0.8)])
    segments = feed_all(seg, pcm)
    assert len(segments) == 1
    s = segments[0]
    # 発話開始 t=1.0 の 200ms 前から始まる
    assert s.t_start == pytest.approx(0.8, abs=0.11)
    # プリロール分の無音が先頭に付き、FakeASR の先頭非ゼロ規約は保たれる
    assert s.pcm[0] == 0
    assert int(s.pcm[s.pcm != 0][0]) == 2000


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


def test_reset_restarts_timeline_at_zero():
    """reset() 後の t_start/t_end は新しいストリームの先頭を 0 とする。

    複数の音源を1つの segmenter で続けて流すとき（ベンチの区切り計測 #24）に、
    前の音源のぶんだけ時刻がずれないこと。
    """
    seg = make_segmenter()
    feed_all(seg, np.concatenate([speech_pcm(2000, 1.0), silence_pcm(0.8)]))
    seg.reset()
    segments = feed_all(seg, np.concatenate([speech_pcm(2000, 1.0), silence_pcm(0.8)]))
    assert len(segments) == 1
    assert segments[0].t_start == pytest.approx(0.0, abs=0.15)
    assert segments[0].t_end == pytest.approx(1.0, abs=0.15)


# ---- #27: start_speech_ms / TurnBoundary ----


def make_morph_segmenter(
    check_silence_ms: int = 320,
    start_speech_ms: int = 96,
    force_silence_ms: int = 640,
) -> VoiceSegmenter:
    """morph 戦略の分割パラメータ（Parapper 実測既定）。"""
    return VoiceSegmenter(
        EnergyVAD(threshold=300),
        sample_rate=SAMPLE_RATE,
        min_silence_ms=check_silence_ms,
        max_utterance_s=30,
        start_speech_ms=start_speech_ms,
        force_silence_ms=force_silence_ms,
    )


def test_simple_params_never_emit_turn_boundary():
    """既定（force_silence_ms=None）では #27 以前と同じく Segment だけが流れる。"""
    seg = make_segmenter()
    events = feed_all(seg, np.concatenate([speech_pcm(2000, 1.0), silence_pcm(3.0)]))
    assert [type(e).__name__ for e in events] == ["Segment"]


def test_boundary_fires_once_after_force_silence():
    seg = make_morph_segmenter()
    events = feed_all(seg, np.concatenate([speech_pcm(2000, 1.0), silence_pcm(3.0)]))
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["Segment", "TurnBoundary"]  # 長い無音でも印は1つだけ
    boundary = events[1]
    # 発話終了(1.0s) から force_silence_ms 後。フレーム粒度(100ms)ぶんの誤差を許容
    assert boundary.at == pytest.approx(1.64, abs=0.11)


def test_short_gap_closes_segment_without_boundary():
    """320ms 以上 640ms 未満の間: Segment は割れるが Turn は割らない（印が出ない）。"""
    seg = make_morph_segmenter()
    pcm = np.concatenate(
        [speech_pcm(1000, 0.6), silence_pcm(0.4), speech_pcm(2000, 0.6), silence_pcm(0.4)]
    )
    events = feed_all(seg, pcm)
    assert [type(e).__name__ for e in events] == ["Segment", "Segment"]


def test_long_gap_closes_segment_and_marks_the_turn():
    seg = make_morph_segmenter()
    pcm = np.concatenate(
        [speech_pcm(1000, 0.6), silence_pcm(0.9), speech_pcm(2000, 0.6), silence_pcm(0.9)]
    )
    kinds = [type(e).__name__ for e in feed_all(seg, pcm)]
    assert kinds == ["Segment", "TurnBoundary", "Segment", "TurnBoundary"]


def test_start_speech_ms_ignores_a_single_false_positive_frame():
    """VAD の誤検知1フレームで発話が立ち上がらない（Parapper B-3）。"""
    seg = make_morph_segmenter(start_speech_ms=300)  # 100msフレーム × 3枚が必要
    pcm = np.concatenate([silence_pcm(0.5), speech_pcm(2000, 0.1), silence_pcm(1.5)])
    segments = [e for e in feed_all(seg, pcm) if isinstance(e, Segment)]
    assert segments == []
    assert seg.flush() is None


def test_start_speech_ms_still_keeps_the_onset_audio():
    """開始判定に使ったフレームは発話に含める（語頭を切り落とさない）。"""
    seg = make_morph_segmenter(start_speech_ms=300)
    pcm = np.concatenate([silence_pcm(0.5), speech_pcm(2000, 1.0), silence_pcm(0.5)])
    segments = [e for e in feed_all(seg, pcm) if isinstance(e, Segment)]
    assert len(segments) == 1
    assert segments[0].t_start == pytest.approx(0.26, abs=0.11)  # プリロール240ms前から
    assert int(segments[0].pcm[segments[0].pcm != 0][0]) == 2000


def test_flush_does_not_emit_a_boundary():
    """flush は無音待ちではないので TurnBoundary を出さない（pipeline 側が打ち切る）。"""
    seg = make_morph_segmenter()
    feed_all(seg, np.concatenate([speech_pcm(2000, 1.0), silence_pcm(0.1)]))
    assert isinstance(seg.flush(), Segment)
    assert feed_all(seg, silence_pcm(3.0)) == []


def test_force_silence_shorter_than_check_silence_is_rejected():
    with pytest.raises(ValueError, match="force_silence_ms"):
        VoiceSegmenter(
            EnergyVAD(threshold=300),
            sample_rate=SAMPLE_RATE,
            min_silence_ms=320,
            force_silence_ms=200,
        )


# ---- interim（partial 字幕の引き金）と発話開始カウンタ（#29） ----


def make_interim_segmenter(interim_silence_ms: int = 96) -> VoiceSegmenter:
    """partial 有効時の分割パラメータ（既定 morph の 500ms + interim 96ms）。"""
    return VoiceSegmenter(
        EnergyVAD(threshold=300),
        sample_rate=SAMPLE_RATE,
        min_silence_ms=500,
        max_utterance_s=30,
        start_speech_ms=96,
        force_silence_ms=1000,
        interim_silence_ms=interim_silence_ms,
    )


def test_interim_disabled_by_default():
    """partial 無効（interim_silence_ms=None）では #29 以前と同じ列が流れる。"""
    seg = make_morph_segmenter()
    pcm = np.concatenate(
        [speech_pcm(1000, 0.6), silence_pcm(0.2), speech_pcm(2000, 0.6), silence_pcm(1.5)]
    )
    kinds = [type(e).__name__ for e in feed_all(seg, pcm)]
    assert "InterimSegment" not in kinds


def test_interim_fires_at_a_mid_utterance_pause_without_splitting():
    """文中の短い間（96ms以上・500ms未満）で interim が1つ出るが、Segment は割れない。

    partial の価値が出るのはこの区間だけ。500ms に達する間はどのみち Segment が閉じるので、
    その interim は「確定の少し前に同じ文を出した」だけになる。
    """
    seg = make_interim_segmenter()
    pcm = np.concatenate(
        [speech_pcm(2000, 0.8), silence_pcm(0.3), speech_pcm(2000, 0.8), silence_pcm(1.5)]
    )
    kinds = [type(e).__name__ for e in feed_all(seg, pcm)]
    # 文中の間で interim → 発話末で interim → Segment 確定 → TurnBoundary
    assert kinds == ["InterimSegment", "InterimSegment", "Segment", "TurnBoundary"]


def test_interim_fires_once_per_silence_gap():
    """1つの無音区間につき1回だけ。長引く間で毎フレーム出すと息継ぎ駆動でなくなる。"""
    seg = make_interim_segmenter()
    events = feed_all(seg, np.concatenate([speech_pcm(2000, 1.0), silence_pcm(3.0)]))
    assert [type(e).__name__ for e in events] == ["InterimSegment", "Segment", "TurnBoundary"]


def test_interim_carries_the_audio_so_far_and_leaves_the_utterance_open():
    seg = make_interim_segmenter()
    feed_all(seg, np.concatenate([speech_pcm(2000, 0.8), silence_pcm(0.15)]))
    assert seg.is_open  # interim は発話を閉じない


def test_interim_audio_is_the_speech_so_far():
    seg = make_interim_segmenter()
    events = feed_all(seg, np.concatenate([speech_pcm(2000, 0.8), silence_pcm(0.15)]))
    interim = events[0]
    assert type(interim).__name__ == "InterimSegment"
    nonzero = interim.pcm[interim.pcm != 0]
    assert nonzero.size > 0 and int(nonzero[0]) == 2000
    assert interim.t_start == pytest.approx(0.0, abs=0.35)  # プリロール込み


def test_interim_not_shorter_than_check_silence_in_frames_is_rejected():
    """ms では違っても切り上げで同じフレーム数に潰れる組合せを弾く。"""
    with pytest.raises(ValueError, match="interim_silence_ms"):
        VoiceSegmenter(
            EnergyVAD(threshold=300),
            sample_rate=SAMPLE_RATE,
            min_silence_ms=100,  # frame_ms=100 なら 1フレーム
            interim_silence_ms=96,  # これも切り上げで 1フレーム
        )


def test_utterances_opened_counts_each_utterance_once():
    """「発話中」インジケーターの引き金。イベント列は増やさずカウンタで伝える。"""
    seg = make_morph_segmenter()
    assert seg.utterances_opened == 0
    feed_all(seg, np.concatenate([speech_pcm(2000, 0.6), silence_pcm(1.5)]))
    assert seg.utterances_opened == 1
    feed_all(seg, np.concatenate([speech_pcm(1000, 0.6), silence_pcm(1.5)]))
    assert seg.utterances_opened == 2


def test_interim_and_forced_close_can_land_on_the_same_frame():
    """max_utterance_s の強制分割と息継ぎが重なると1バッチに2つ出る。

    このとき interim は確定 Segment と同じ音声を**先に** ASR へ通すことになり、
    確定字幕を1回ぶん遅らせる。Pipeline 側がこのケースの interim を落とす根拠。
    """
    seg = VoiceSegmenter(
        EnergyVAD(threshold=300),
        sample_rate=SAMPLE_RATE,
        min_silence_ms=500,
        max_utterance_s=1,  # 1秒で強制分割
        interim_silence_ms=96,
    )
    events = feed_all(seg, np.concatenate([speech_pcm(2000, 0.9), silence_pcm(0.2)]))
    assert [type(e).__name__ for e in events][:2] == ["InterimSegment", "Segment"]
