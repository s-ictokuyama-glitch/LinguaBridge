"""sherpa-onnx + ReazonSpeech K2 v2 による ASREngine 実装（イシュー#28）。

日本語特化の zipformer transducer（RNN-T）。whisper 系と違い 30秒パディングが無く、
デコード時間が音声長にほぼ比例する（#28 の実測で固定費 0.02s / RTF 中央値 0.024）。

**このエンジンは句読点を出力しない**。#21 で語彙 5,224 トークンを走査した結果、
`。` `、` `！` `？` はいずれも存在しない。`turn.classifier: surface` は ASR テキストの
末尾の `。！？` を見るため、このエンジンと組み合わせるときは文法境界の判定手段が別に要る。

**リードイン（`lead_in_ms`）が精度を支配する**。#28 の実測で、発話が音声の先頭から
いきなり始まると encoder が冒頭を取りこぼす（拡張コーパスの CER が 19.70%）。
無音を先頭に足すと 600ms 付近でプラトーに入り 3.48% まで下がる。上流の
`vad.pre_roll_ms`(240ms) だけでは足りないので、**このエンジン自身が不足分を足す**。

`dither` は 0.0 から動かさないこと。1.0（kaldi の fbank 既定）にすると CER が 99% に
なる — このモデルは dither 無しで学習されている（#28 実測）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

from server.asr.base import ASREngine, ASRResult

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000

# Int8Float32（encoder=int8 / decoder=fp32 / joiner=int8）。Parapper の既定と同じ構成。
# decoder だけ fp32 なのはサイズが 11MB と小さく、int8 化の得が無いため
ENCODER_FILE = "encoder-epoch-99-avg-1.int8.onnx"
DECODER_FILE = "decoder-epoch-99-avg-1.onnx"
DECODER_INT8_FILE = "decoder-epoch-99-avg-1.int8.onnx"
JOINER_FILE = "joiner-epoch-99-avg-1.int8.onnx"
TOKENS_FILE = "tokens.txt"
# 補完で再デコードする区間の上限。これより長い区間は読み直しても同じ脱落が起きるうえ、
# 雑音の孤立トークンに挟まれた無音を丸ごと読み直すことになる
REFILL_MAX_S = 8.0


class SherpaOnnxEngine(ASREngine):
    """ReazonSpeech K2 v2 のオフライン認識器。

    transducer は whisper の `avg_logprob` / `no_speech_prob` / `compression_ratio` を
    持たないので、幻覚フィルタ（E-04）には**中立値**を返す。空文字のときだけ
    `no_speech_prob=1.0` にして "empty" 判定に乗せる。
    無音で定型文を吐かないことは #28 で実測済みで、フィルタ側の閾値には頼らない。
    """

    def __init__(
        self,
        model_dir: Path,
        num_threads: int = 4,
        decoding_method: str = "greedy_search",
        lead_in_ms: int = 600,
        hotwords_file: Path | None = None,
        hotwords_score: float = 1.5,
        blank_penalty: float = 0.0,
        refill_gap_s: float = 0.0,
        int8_decoder: bool = False,
    ) -> None:
        decoder = DECODER_INT8_FILE if int8_decoder else DECODER_FILE
        required = [ENCODER_FILE, decoder, JOINER_FILE, TOKENS_FILE]
        missing = [f for f in required if not (model_dir / f).exists()]
        if missing:
            # fail closed。クラウドへ逃げない（絶対要件）
            raise FileNotFoundError(
                f"ReazonSpeech モデルが見つからない: {model_dir}（不足: {missing}）\n"
                "python scripts/download_models.py --only reazonspeech を実行してください"
            )
        if lead_in_ms < 0:
            raise ValueError("lead_in_ms は0以上にすること")
        if blank_penalty < 0:
            raise ValueError("blank_penalty は0以上にすること")
        if refill_gap_s < 0:
            raise ValueError("refill_gap_s は0以上にすること")
        self._model_dir = model_dir
        self._decoder_file = decoder
        # 0 = 「ライブラリ既定」を意味する asr.cpu_threads の規約に合わせる。
        # sherpa-onnx の num_threads に 0 は渡せないので既定値へ畳む
        self._num_threads = num_threads if num_threads > 0 else 4
        self._decoding_method = decoding_method
        self._lead_in = np.zeros(SAMPLE_RATE * lead_in_ms // 1000, dtype=np.float32)
        self._hotwords_file = hotwords_file
        self._hotwords_score = hotwords_score
        self._blank_penalty = blank_penalty
        self._refill_gap_s = refill_gap_s
        self._recognizer: Any = None

    def warmup(self) -> None:
        """モデルロード＋ダミー推論。冪等。"""
        import sherpa_onnx

        if self._recognizer is not None:
            return
        logger.info("ASRモデルをロード中: %s (sherpa-onnx)", self._model_dir.name)
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(self._model_dir / ENCODER_FILE),
            decoder=str(self._model_dir / self._decoder_file),
            joiner=str(self._model_dir / JOINER_FILE),
            tokens=str(self._model_dir / TOKENS_FILE),
            num_threads=self._num_threads,
            provider="cpu",
            decoding_method=self._decoding_method,
            # hotwords は modified_beam_search でしか効かない（sherpa-onnx の仕様）
            hotwords_file=str(self._hotwords_file) if self._hotwords_file else "",
            hotwords_score=self._hotwords_score,
            blank_penalty=self._blank_penalty,
            dither=0.0,  # 上の docstring 参照。動かすと壊れる
        )
        self.transcribe(np.zeros(SAMPLE_RATE, dtype=np.int16), SAMPLE_RATE)
        logger.info("ASRウォームアップ完了")

    def transcribe(self, pcm16: np.ndarray, sample_rate: int) -> ASRResult:
        if sample_rate != SAMPLE_RATE:
            raise ValueError(f"サンプルレートは{SAMPLE_RATE}固定（got {sample_rate}）")
        if self._recognizer is None:  # 通常は起動時 warmup 済み。直接利用時の保険
            self.warmup()
        audio = pcm16.astype(np.float32) / 32768.0
        result = self._decode(audio)
        text = self._refill_gaps(audio, result) if self._refill_gap_s > 0 else result.text.strip()
        if not text:
            return ASRResult(text="", no_speech_prob=1.0)
        # transducer に確信度の相当物が無いため、幻覚フィルタの3つの数値判定は
        # すべて通す中立値を返す（既定値のまま）。判定は既知フレーズ辞書だけが効く
        return ASRResult(text=text)

    def _decode(self, audio: np.ndarray) -> Any:
        # 冒頭の取りこぼし対策。上流の pre_roll と足りない分ではなく常に足す —
        # 呼び出し側が pre_roll を持つかどうかにこのエンジンの精度を依存させない
        stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, np.concatenate([self._lead_in, audio]))
        self._recognizer.decode_stream(stream)
        return stream.result

    def _refill_gaps(self, audio: np.ndarray, result: Any) -> str:
        """トークンが `refill_gap_s` 以上出ない区間を、その区間だけ再デコードして補う。

        このモデルは長い発話を受け取ると、単独なら認識できる冒頭の文を丸ごと出さない
        （授業動画の実測。音量・探索法・リードイン長では直らず、後ろに続く発話の量で起きる）。
        短く切ると文脈が減って用語の誤りが増えるので、全体のデコードは残し、空白だけを埋める。
        本当の無音の空白は再デコードしても空文字になるだけ。
        """
        lead_s = self._lead_in.size / SAMPLE_RATE
        tokens = list(result.tokens)
        times = [t - lead_s for t in result.timestamps]
        if not tokens:
            return ""
        # 冒頭の孤立した1〜2文字（次まで空白が続く）は雑音の取りこぼしなので、冒頭の再デコードに含める
        first = 0
        while first < 2 and first + 1 < len(times) and times[first + 1] - times[first] >= self._refill_gap_s:
            first += 1
        if self._refill_gap_s <= times[first] <= REFILL_MAX_S:
            pieces = [self._decode(audio[: int(times[first] * SAMPLE_RATE)]).text.strip()]
        else:
            pieces = tokens[:first]
        for i in range(first, len(tokens)):
            pieces.append(tokens[i])
            if i + 1 < len(tokens) and self._refill_gap_s <= times[i + 1] - times[i] <= REFILL_MAX_S:
                # 直前のトークンの音を二重に読まないよう、トークン1つ分（約0.12秒）後ろから切る
                start = int((times[i] + 0.12) * SAMPLE_RATE)
                pieces.append(self._decode(audio[start : int(times[i + 1] * SAMPLE_RATE)]).text.strip())
        return "".join(pieces).strip()
