"""faster-whisper による ASREngine 実装（イシュー#11）。

モデルは models.dir 配下のディレクトリ名で指定し、config.yaml の変更のみで
faster-whisper-small ⇄ kotoba-whisper-v2.0-faster を切替できる。
既定は判断ゲート①の確定値 small（docs/bench/2026-07-07-bench.md）。
"""

from __future__ import annotations

import logging
import statistics
from pathlib import Path
from typing import Any

import numpy as np

from server.asr.base import ASREngine, ASRResult

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class FasterWhisperEngine(ASREngine):
    def __init__(
        self, model_dir: Path, compute_type: str = "int8", language: str = "ja"
    ) -> None:
        if not model_dir.exists():
            raise FileNotFoundError(
                f"ASRモデルが見つからない: {model_dir}\n"
                "scripts/download_models.py を実行してモデルを取得してください"
            )
        self._model_dir = model_dir
        self._compute_type = compute_type
        self._language = language
        self._model: Any = None

    def warmup(self) -> None:
        """モデルロード＋ダミー推論。初回発話の遅延スパイクを防ぐ（起動時に呼ぶ）。"""
        from faster_whisper import WhisperModel

        logger.info("ASRモデルをロード中: %s (%s)", self._model_dir.name, self._compute_type)
        self._model = WhisperModel(
            str(self._model_dir), device="cpu", compute_type=self._compute_type
        )
        self.transcribe(np.zeros(SAMPLE_RATE, dtype=np.int16), SAMPLE_RATE)
        logger.info("ASRウォームアップ完了")

    def transcribe(self, pcm16: np.ndarray, sample_rate: int) -> ASRResult:
        if sample_rate != SAMPLE_RATE:
            raise ValueError(f"サンプルレートは{SAMPLE_RATE}固定（got {sample_rate}）")
        if self._model is None:  # 通常は起動時 warmup 済み。直接利用時の保険
            self.warmup()
        audio = pcm16.astype(np.float32) / 32768.0
        segments, _info = self._model.transcribe(
            audio,
            language=self._language,
            beam_size=1,
            vad_filter=False,  # 発話区切りは上流の VoiceSegmenter が担う
            condition_on_previous_text=False,  # 発話単位で独立させ幻覚の連鎖を防ぐ
        )
        segs = list(segments)
        if not segs:
            return ASRResult(text="", no_speech_prob=1.0)
        return ASRResult(
            text="".join(s.text for s in segs).strip(),
            avg_logprob=statistics.fmean(s.avg_logprob for s in segs),
            no_speech_prob=statistics.fmean(s.no_speech_prob for s in segs),
            compression_ratio=statistics.fmean(s.compression_ratio for s in segs),
        )
