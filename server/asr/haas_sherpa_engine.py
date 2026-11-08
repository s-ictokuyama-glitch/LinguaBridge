from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from server.asr.base import ASREngine, ASRResult

logger = logging.getLogger(__name__)
SAMPLE_RATE = 16000
MODEL_FILES = {
    "encoder.onnx": 41_836_973,
    "encoder.data": 2_435_420_160,
    "decoder.onnx": 15_661_602,
    "joiner.onnx": 12_158_641,
    "tokens.txt": 28_704,
}


class HaasSherpaEngine(ASREngine):
    def __init__(
        self,
        model_dir: Path,
        *,
        num_threads: int = 4,
        decoding_method: str = "greedy_search",
        hotwords_file: Path | None = None,
        hotwords_score: float = 1.5,
    ) -> None:
        for name, size in MODEL_FILES.items():
            path = model_dir / name
            if not path.is_file() or path.stat().st_size != size:
                raise FileNotFoundError(f"Haas-ja (sherpa) モデルが見つからないか不完全です: {path}")
        if hotwords_file is not None and not hotwords_file.is_file():
            raise FileNotFoundError(f"Haas-ja の hotwords ファイルが見つからない: {hotwords_file}")
        self._model_dir = model_dir
        self._num_threads = num_threads or 4
        self._decoding_method = decoding_method
        self._hotwords_file = hotwords_file
        self._hotwords_score = hotwords_score
        self._recognizer = None

    def warmup(self) -> None:
        if self._recognizer is not None:
            return
        import sherpa_onnx

        logger.info("ASRモデルをロード中: %s (sherpa-onnx NeMo TDT)", self._model_dir.name)
        recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(self._model_dir / "encoder.onnx"),
            decoder=str(self._model_dir / "decoder.onnx"),
            joiner=str(self._model_dir / "joiner.onnx"),
            tokens=str(self._model_dir / "tokens.txt"),
            model_type="nemo_transducer",
            feature_dim=128,
            num_threads=self._num_threads,
            provider="cpu",
            decoding_method=self._decoding_method,
            hotwords_file=str(self._hotwords_file) if self._hotwords_file else "",
            hotwords_score=self._hotwords_score,
        )
        self._decode(recognizer, np.zeros(SAMPLE_RATE, dtype=np.float32))
        self._recognizer = recognizer

    @staticmethod
    def _decode(recognizer, audio: np.ndarray) -> str:
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio)
        recognizer.decode_stream(stream)
        return stream.result.text.strip()

    def transcribe(self, pcm16: np.ndarray, sample_rate: int) -> ASRResult:
        if sample_rate != SAMPLE_RATE:
            raise ValueError(f"サンプルレートは{SAMPLE_RATE}固定（got {sample_rate}）")
        if pcm16.size == 0:
            return ASRResult(text="", no_speech_prob=1.0)
        self.warmup()
        text = self._decode(self._recognizer, pcm16.astype(np.float32) / 32768.0)
        return ASRResult(text=text) if text else ASRResult(text="", no_speech_prob=1.0)
