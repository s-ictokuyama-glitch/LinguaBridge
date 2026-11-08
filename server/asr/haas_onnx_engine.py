from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import numpy as np

from server.asr.base import ASREngine, ASRResult

SAMPLE_RATE = 16000
HOP_LENGTH = 160
FFT_SIZE = 512
MANIFEST_SHA256 = "93a64b20a6270fc6a75a897a869aa8d24594580321fb7e0ec814e2a5fe09e83d"
FORMATS = {"int4": "int4-onecomp", "int8": "int8"}


def _chunks(audio: np.ndarray) -> Iterator[np.ndarray]:
    target = 28 * SAMPLE_RATE
    search = 2 * SAMPLE_RATE
    half_window = SAMPLE_RATE // 20
    while audio.size > target:
        first_cut = target - search
        last_cut = min(target + search, audio.size - half_window)
        local = audio[first_cut - half_window : last_cut + half_window]
        energy = np.concatenate(([0.0], np.cumsum(local.astype(np.float64) ** 2)))
        window = 2 * half_window
        cut = first_cut + int(np.argmin(energy[window:] - energy[:-window]))
        yield audio[:cut]
        audio = audio[cut:]
    yield audio


def verified_model_paths(
    model_dir: Path, quantization: Literal["int4", "int8"], *, additional_files: tuple[str, ...] = (),
) -> dict[str, Path]:
    manifest_path = model_dir / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != MANIFEST_SHA256:
            raise ValueError(f"Haas-ja の manifest が固定版と一致しません: {manifest_path}")
        manifest = json.loads(manifest_bytes)
        encoder = manifest["formats"][FORMATS[quantization]]["encoder"]
        files = {"encoder": encoder, "weights": f"{encoder}.data", **manifest["common"]}
        for name in (*files.values(), *additional_files):
            path = model_dir / name
            expected = manifest["files"][name]
            if path.stat().st_size != expected["bytes"]:
                raise ValueError(f"Haas-ja モデルのサイズが一致しません: {path}")
            with path.open("rb") as stream:
                sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
            if sha256 != expected["sha256"]:
                raise ValueError(f"Haas-ja モデルの SHA256 が一致しません: {path}")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Haas-ja の必須ファイルが見つかりません: {exc.filename}") from exc
    return {key: model_dir / name for key, name in files.items()}


class _HaasDecoder:
    def __init__(self, paths: dict[str, Path], threads: int) -> None:
        import onnxruntime as ort  # type: ignore[import-untyped]

        def session(key: str, num_threads: int) -> ort.InferenceSession:
            options = ort.SessionOptions()
            options.intra_op_num_threads = num_threads
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            return ort.InferenceSession(str(paths[key]), options, providers=["CPUExecutionProvider"])

        frontend = json.loads(paths["frontend"].read_text(encoding="utf-8"))
        self._window = np.zeros(FFT_SIZE, dtype=np.float64)
        window = frontend["window"]
        offset = (FFT_SIZE - len(window)) // 2
        self._window[offset : offset + len(window)] = window
        self._mel_filter = np.asarray(frontend["mel_filter"], dtype=np.float32)
        self._preemphasis = np.float32(frontend["preemph"])
        self._log_floor = np.float32(frontend["log_zero_guard_value"])
        self._pad_to = frontend["pad_to"]

        decoding = json.loads(paths["decoding"].read_text(encoding="utf-8"))
        self._blank = decoding["blank_id"]
        self._durations = decoding["durations"]
        self._max_symbols = decoding["max_symbols"]
        tokens = json.loads(paths["tokens"].read_text(encoding="utf-8"))
        self._pieces = tokens["pieces"]
        self._unknown = tokens["unk_id"]

        self._encoder = session("encoder", threads)
        self._prediction = session("prediction", 1)
        self._joint = session("joint", 1)
        self._start = self._predict(
            self._blank,
            np.zeros((2, 1, 5, 64, 64), dtype=np.float32),
            np.zeros((2, 1, 3, 320, 4), dtype=np.float32),
        )

    def _features(self, audio: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if audio.size < 2 * HOP_LENGTH:
            audio = np.pad(audio, (0, 2 * HOP_LENGTH - audio.size))
        valid = audio.size // HOP_LENGTH
        samples = audio.copy()
        samples[1:] -= self._preemphasis * audio[:-1]
        padded = np.pad(samples, FFT_SIZE // 2)
        frames = np.lib.stride_tricks.sliding_window_view(padded, FFT_SIZE)[::HOP_LENGTH]
        power = (np.abs(np.fft.rfft(frames * self._window)) ** 2).astype(np.float32)
        mel = np.log(self._mel_filter @ power.T + self._log_floor)
        active = mel[:, :valid]
        mel[:, :valid] = (active - active.mean(axis=1, keepdims=True)) / (
            active.std(axis=1, ddof=1, keepdims=True) + np.float32(1e-5)
        )
        mel[:, valid:] = 0
        mel = np.pad(mel, ((0, 0), (0, -mel.shape[1] % self._pad_to)))
        return np.ascontiguousarray(mel[None]), np.array([valid], dtype=np.int64)

    def _predict(
        self, token: int, kda: np.ndarray, conv: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        projection, kda, conv = self._prediction.run(
            ["pred_proj", "kda_state_out", "conv_state_out"],
            {"token": np.array([token], dtype=np.int64), "kda_state": kda, "conv_state": conv},
        )
        return projection, kda, conv

    def _decode(self, encoded: np.ndarray) -> Iterator[int]:
        projection, kda, conv = self._start
        frame = symbols_at_frame = 0
        while frame < len(encoded):
            (logits,) = self._joint.run(
                ["logits"], {"enc_frame": encoded[frame : frame + 1], "pred_proj": projection},
            )
            scores = logits[0]
            token = int(np.argmax(scores[: self._blank + 1]))
            duration = self._durations[int(np.argmax(scores[self._blank + 1 :]))]
            if token != self._blank:
                yield token
                projection, kda, conv = self._predict(token, kda, conv)
                symbols_at_frame += 1
            if duration or token == self._blank or symbols_at_frame >= self._max_symbols:
                frame += max(1, duration)
                symbols_at_frame = 0

    def _text(self, ids: Iterator[int]) -> str:
        text = []
        pending = bytearray()
        for token in ids:
            piece = self._pieces[token]
            if piece.startswith("<0x") and piece.endswith(">") and len(piece) == 6:
                pending.append(int(piece[3:5], 16))
                continue
            if pending:
                text.append(pending.decode("utf-8", errors="replace"))
                pending.clear()
            text.append(" ⁇ " if token == self._unknown else piece.replace("▁", " "))
        text.append(pending.decode("utf-8", errors="replace"))
        return "".join(text).strip()

    def transcribe(self, audio: np.ndarray) -> str:
        mel, length = self._features(audio)
        encoded, frames = self._encoder.run(
            ["enc_proj", "enc_length"], {"mel": mel, "mel_length": length},
        )
        return self._text(self._decode(np.ascontiguousarray(encoded[0, : int(frames[0])])))


class HaasOnnxEngine(ASREngine):
    def __init__(
        self, model_dir: Path, *, quantization: Literal["int4", "int8"] = "int4", num_threads: int = 4,
    ) -> None:
        self._model_dir = model_dir
        self._quantization = quantization
        self._num_threads = num_threads or 4
        self._decoder: _HaasDecoder | None = None

    def warmup(self) -> None:
        if self._decoder is None:
            paths = verified_model_paths(self._model_dir, self._quantization)
            decoder = _HaasDecoder(paths, self._num_threads)
            decoder.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32))
            self._decoder = decoder

    def transcribe(self, pcm16: np.ndarray, sample_rate: int) -> ASRResult:
        if sample_rate != SAMPLE_RATE:
            raise ValueError(f"サンプルレートは{SAMPLE_RATE}固定（got {sample_rate}）")
        if pcm16.size == 0:
            return ASRResult(text="", no_speech_prob=1.0)
        self.warmup()
        assert self._decoder is not None
        audio = pcm16.astype(np.float32) / 32768.0
        text = "".join(self._decoder.transcribe(chunk) for chunk in _chunks(audio))
        return ASRResult(text=text) if text else ASRResult(text="", no_speech_prob=1.0)
