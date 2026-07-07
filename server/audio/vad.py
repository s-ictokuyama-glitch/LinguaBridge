"""発話セグメンテーション。

VoiceSegmenter はフレーム単位のVAD判定から発話セグメントを組み立てる状態機械:
無音 min_silence_ms 継続で発話確定、max_utterance_s で強制分割（plan.md E-03）。

フレーム判定器は差し替え可能で、#10 では EnergyVAD（RMS閾値）、
#11 で Silero VAD に置換する（インターフェース互換）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import numpy as np


class FrameVAD(Protocol):
    def is_speech(self, frame: np.ndarray) -> bool: ...


class EnergyVAD:
    """RMSエネルギーによる暫定VAD（int16スケールの閾値）。"""

    def __init__(self, threshold: float = 300.0) -> None:
        self.threshold = threshold

    def is_speech(self, frame: np.ndarray) -> bool:
        if frame.size == 0:
            return False
        rms = float(np.sqrt(np.mean(np.square(frame.astype(np.float64)))))
        return rms > self.threshold


@dataclass
class Segment:
    pcm: np.ndarray  # int16 mono
    t_start: float  # 音声先頭からの秒
    t_end: float  # 最後に音声を検出したフレームの末尾（秒）
    closed_at: float  # time.monotonic()。delay_ms 計測の起点
    forced: bool = False  # max_utterance_s による強制分割か


class VoiceSegmenter:
    def __init__(
        self,
        vad: FrameVAD,
        *,
        sample_rate: int = 16000,
        min_silence_ms: int = 500,
        max_utterance_s: int = 30,
        frame_ms: int = 100,
    ) -> None:
        self._vad = vad
        self._sample_rate = sample_rate
        self._frame_len = sample_rate * frame_ms // 1000
        self._silence_frames_to_close = max(1, min_silence_ms // frame_ms)
        self._max_samples = max_utterance_s * sample_rate
        self._pending = np.zeros(0, dtype=np.int16)  # フレーム長未満の端数
        self._offset = 0  # 音声先頭からの処理済みサンプル数
        self._frames: list[np.ndarray] = []
        self._utt_start_sample = 0
        self._last_speech_end_sample = 0
        self._silence_run = 0

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def feed(self, pcm: np.ndarray) -> list[Segment]:
        segments: list[Segment] = []
        buf = np.concatenate([self._pending, pcm]) if self._pending.size else pcm
        n_frames = buf.size // self._frame_len
        for i in range(n_frames):
            frame = buf[i * self._frame_len : (i + 1) * self._frame_len]
            segment = self._process_frame(frame)
            if segment is not None:
                segments.append(segment)
        self._pending = np.array(buf[n_frames * self._frame_len :], dtype=np.int16)
        return segments

    def flush(self) -> Segment | None:
        """進行中の発話を強制確定する（一時停止・終了時に呼ぶ）。"""
        self._pending = np.zeros(0, dtype=np.int16)
        if not self._frames:
            return None
        return self._close(forced=False)

    def reset(self) -> None:
        self._pending = np.zeros(0, dtype=np.int16)
        self._reset_utterance()

    def _reset_utterance(self) -> None:
        self._frames = []
        self._utt_start_sample = 0
        self._last_speech_end_sample = 0
        self._silence_run = 0

    def _process_frame(self, frame: np.ndarray) -> Segment | None:
        frame_start = self._offset
        self._offset += frame.size
        speech = self._vad.is_speech(frame)

        if not self._frames:
            if not speech:
                return None  # 発話外の無音は捨てる
            self._utt_start_sample = frame_start
            self._frames = [frame]
            self._last_speech_end_sample = self._offset
            self._silence_run = 0
            return self._maybe_force_close()

        self._frames.append(frame)
        if speech:
            self._silence_run = 0
            self._last_speech_end_sample = self._offset
        else:
            self._silence_run += 1
            if self._silence_run >= self._silence_frames_to_close:
                return self._close(forced=False)
        return self._maybe_force_close()

    def _maybe_force_close(self) -> Segment | None:
        if self._offset - self._utt_start_sample >= self._max_samples:
            return self._close(forced=True)
        return None

    def _close(self, forced: bool) -> Segment:
        segment = Segment(
            pcm=np.concatenate(self._frames),
            t_start=self._utt_start_sample / self._sample_rate,
            t_end=self._last_speech_end_sample / self._sample_rate,
            closed_at=time.monotonic(),
            forced=forced,
        )
        self._reset_utterance()
        return segment
