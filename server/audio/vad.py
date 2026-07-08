"""発話セグメンテーション。

VoiceSegmenter はフレーム単位のVAD判定から発話セグメントを組み立てる状態機械:
無音 min_silence_ms 継続で発話確定、max_utterance_s で強制分割（plan.md E-03）。

フレーム判定器は差し替え可能: 本番は SileroVAD（ONNX, 32msフレーム）、
テスト・フォールバック用に EnergyVAD（RMS閾値, 100msフレーム）。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import numpy as np

if TYPE_CHECKING:
    from server.config import VadConfig


class FrameVAD(Protocol):
    frame_ms: int  # この実装が要求する判定フレーム長

    def is_speech(self, frame: np.ndarray) -> bool: ...

    def reset(self) -> None: ...


class EnergyVAD:
    """RMSエネルギーによるVAD（int16スケールの閾値）。テスト・フォールバック用。"""

    frame_ms = 100

    def __init__(self, threshold: float = 300.0) -> None:
        self.threshold = threshold

    def is_speech(self, frame: np.ndarray) -> bool:
        if frame.size == 0:
            return False
        rms = float(np.sqrt(np.mean(np.square(frame.astype(np.float64)))))
        return rms > self.threshold

    def reset(self) -> None:
        pass


class SileroVAD:
    """Silero VAD（faster-whisper 同梱の ONNX モデル）のストリーミング利用。

    512サンプル（32ms @16kHz）単位で音声確率を返す。同梱の SileroVADModel API は
    バッチ指向で呼び出しごとにRNN状態がリセットされるため、ONNXセッションを直接呼び、
    h/c 状態と直前64サンプルの文脈を自前で保持する（faster-whisper 1.2系で動作確認）。
    """

    FRAME_SAMPLES = 512
    _CONTEXT_SAMPLES = 64
    frame_ms = 32  # 512サンプル @16kHz

    def __init__(self, threshold: float = 0.5) -> None:
        from faster_whisper.vad import get_vad_model

        session = getattr(get_vad_model(), "session", None)
        if session is None:
            raise RuntimeError(
                "faster-whisper の Silero VAD 内部API（SileroVADModel.session）が"
                "見つからない。faster-whisper のバージョン変更が原因の可能性。"
                "requirements.txt のバージョン指定と本クラスの実装を確認のこと"
            )
        self._session = session
        self.threshold = threshold
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(self._CONTEXT_SAMPLES, dtype=np.float32)

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(self._CONTEXT_SAMPLES, dtype=np.float32)

    def is_speech(self, frame: np.ndarray) -> bool:
        audio = frame.astype(np.float32) / 32768.0
        if audio.size != self.FRAME_SAMPLES:  # 防御。VoiceSegmenter は常に固定長を渡す
            padded = np.zeros(self.FRAME_SAMPLES, dtype=np.float32)
            padded[: min(audio.size, self.FRAME_SAMPLES)] = audio[: self.FRAME_SAMPLES]
            audio = padded
        model_input = np.concatenate([self._context, audio])[None, :]
        out, self._h, self._c = self._session.run(
            None, {"input": model_input, "h": self._h, "c": self._c}
        )
        self._context = audio[-self._CONTEXT_SAMPLES:]
        return float(np.asarray(out).reshape(-1)[0]) > self.threshold


def build_frame_vad(vad_config: VadConfig) -> FrameVAD:
    """設定から FrameVAD 実装を作る。フレーム長は各実装の frame_ms 属性が持つ。"""
    if vad_config.engine == "silero":
        return SileroVAD(vad_config.threshold)
    if vad_config.engine == "energy":
        if vad_config.threshold <= 1.0:
            # silero用の確率閾値のまま energy に切り替えると全フレームが音声判定になる
            raise ValueError(
                f"energy VAD の threshold は int16 RMS スケール（例: 300）。"
                f"現在値 {vad_config.threshold} は silero 用の確率閾値の可能性"
            )
        return EnergyVAD(vad_config.threshold)
    raise ValueError(f"未知のVADエンジン: {vad_config.engine}")


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
        pre_roll_ms: int = 240,
    ) -> None:
        self._vad = vad
        self._sample_rate = sample_rate
        self._frame_len = sample_rate * frame_ms // 1000
        # 切り上げ: フレーム長で割り切れない場合も「min_silence_ms 以上の無音」を保証する
        self._silence_frames_to_close = max(1, -(-min_silence_ms // frame_ms))
        self._max_samples = max_utterance_s * sample_rate
        self._pending = np.zeros(0, dtype=np.int16)  # フレーム長未満の端数
        self._offset = 0  # 音声先頭からの処理済みサンプル数
        self.last_speech_at: float | None = None  # 最後に音声を検出した時刻（無音警告 E-01 用）
        self._frames: list[np.ndarray] = []
        self._utt_start_sample = 0
        self._last_speech_end_sample = 0
        self._silence_run = 0
        # プリロール: 発話開始判定の直前の音声を発話に含める（語頭の欠けを防ぐ。
        # VAD判定はフレーム粒度なので、開始フレームだけだと立ち上がりの子音が削れる）
        self._pre_roll: deque[np.ndarray] = deque(maxlen=max(0, pre_roll_ms // frame_ms))

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
        self._pre_roll.clear()
        self._vad.reset()
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
        if speech:
            self.last_speech_at = time.monotonic()

        if not self._frames:
            if not speech:
                self._pre_roll.append(frame)  # 次の発話のプリロール候補として保持
                return None
            preceding = list(self._pre_roll)
            self._pre_roll.clear()
            self._utt_start_sample = frame_start - sum(f.size for f in preceding)
            self._frames = [*preceding, frame]
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
