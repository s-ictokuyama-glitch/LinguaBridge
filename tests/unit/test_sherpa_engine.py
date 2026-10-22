"""SherpaOnnxEngine のモデル非依存な単体テスト（イシュー#28）。

実モデルを使う検証は tests/integration/test_real_asr.py 側にある。
ここで守りたいのは「モデルが無いときにクラウドへ逃げない」ことと、
**#28 の実測で精度を支配すると分かったリードインが実際に足されている**こと。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from server.asr.sherpa_engine import (
    DECODER_FILE,
    DECODER_INT8_FILE,
    ENCODER_FILE,
    JOINER_FILE,
    TOKENS_FILE,
    SAMPLE_RATE,
    SherpaOnnxEngine,
)
from server.config import AsrConfig, SherpaConfig


def make_model_dir(tmp_path: Path, *, files: tuple[str, ...] | None = None) -> Path:
    model_dir = tmp_path / "reazonspeech-k2-v2"
    model_dir.mkdir()
    for name in files if files is not None else (ENCODER_FILE, DECODER_FILE, JOINER_FILE, TOKENS_FILE):
        (model_dir / name).write_bytes(b"stub")
    return model_dir


class TestFailClosed:
    """モデルが無いときは必ず失敗する。自動ダウンロードもクラウドfallbackもしない。"""

    def test_missing_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            SherpaOnnxEngine(tmp_path / "not-there")

    def test_missing_file_is_named_in_the_error(self, tmp_path: Path) -> None:
        model_dir = make_model_dir(tmp_path, files=(ENCODER_FILE, DECODER_FILE, TOKENS_FILE))
        with pytest.raises(FileNotFoundError, match=JOINER_FILE):
            SherpaOnnxEngine(model_dir)

    def test_int8_decoder_requires_the_int8_file(self, tmp_path: Path) -> None:
        """全int8 構成を選んだら fp32 の decoder があっても足りない。"""
        model_dir = make_model_dir(tmp_path)  # fp32 decoder だけがある
        with pytest.raises(FileNotFoundError, match=DECODER_INT8_FILE):
            SherpaOnnxEngine(model_dir, int8_decoder=True)


class TestLeadIn:
    """#28 実測: 発話がいきなり始まると encoder が冒頭を落とす（CER 19.70% → 3.48%）。

    上流の `vad.pre_roll_ms` に依存せず、このエンジン自身が足していることを確かめる。
    """

    def test_lead_in_is_prepended_to_the_audio(self, tmp_path: Path, monkeypatch) -> None:
        engine = SherpaOnnxEngine(make_model_dir(tmp_path), lead_in_ms=600)
        seen: dict[str, np.ndarray] = {}
        engine._recognizer = _FakeRecognizer(seen, text="テストです")  # type: ignore[assignment]

        pcm = np.full(SAMPLE_RATE, 1000, dtype=np.int16)  # 1秒ぶんの非ゼロ
        engine.transcribe(pcm, SAMPLE_RATE)

        audio = seen["audio"]
        lead = SAMPLE_RATE * 600 // 1000
        assert audio.size == pcm.size + lead
        assert np.all(audio[:lead] == 0.0)  # 足されたのは無音
        assert np.all(audio[lead:] != 0.0)  # 元の音声は欠けていない

    def test_zero_lead_in_passes_the_audio_through(self, tmp_path: Path) -> None:
        engine = SherpaOnnxEngine(make_model_dir(tmp_path), lead_in_ms=0)
        seen: dict[str, np.ndarray] = {}
        engine._recognizer = _FakeRecognizer(seen, text="テストです")  # type: ignore[assignment]

        pcm = np.full(SAMPLE_RATE, 1000, dtype=np.int16)
        engine.transcribe(pcm, SAMPLE_RATE)
        assert seen["audio"].size == pcm.size

    def test_negative_lead_in_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            SherpaOnnxEngine(make_model_dir(tmp_path), lead_in_ms=-1)


class TestResult:
    def test_empty_text_reports_no_speech(self, tmp_path: Path) -> None:
        """transducer に no_speech_prob は無いので、空のときだけ 1.0 を立てる。

        これで幻覚フィルタ（E-04）の "empty" 判定に乗る。
        """
        engine = SherpaOnnxEngine(make_model_dir(tmp_path))
        engine._recognizer = _FakeRecognizer({}, text="   ")  # type: ignore[assignment]
        result = engine.transcribe(np.zeros(SAMPLE_RATE, dtype=np.int16), SAMPLE_RATE)
        assert result.text == ""
        assert result.no_speech_prob == 1.0

    def test_non_empty_text_passes_the_numeric_hallucination_checks(self, tmp_path: Path) -> None:
        """確信度の相当物が無いため、数値3項目は中立値で通す（判定は既知フレーズ辞書のみ）。"""
        from server.asr.hallucination import hallucination_reason

        engine = SherpaOnnxEngine(make_model_dir(tmp_path))
        engine._recognizer = _FakeRecognizer({}, text="光合成には日光が必要です")  # type: ignore[assignment]
        result = engine.transcribe(np.zeros(SAMPLE_RATE, dtype=np.int16), SAMPLE_RATE)
        assert hallucination_reason(result) is None

    def test_known_hallucination_phrase_is_still_caught(self, tmp_path: Path) -> None:
        from server.asr.hallucination import hallucination_reason

        engine = SherpaOnnxEngine(make_model_dir(tmp_path))
        engine._recognizer = _FakeRecognizer({}, text="ご視聴ありがとうございました")  # type: ignore[assignment]
        result = engine.transcribe(np.zeros(SAMPLE_RATE, dtype=np.int16), SAMPLE_RATE)
        assert hallucination_reason(result) == "known_phrase"

    def test_wrong_sample_rate_is_rejected(self, tmp_path: Path) -> None:
        engine = SherpaOnnxEngine(make_model_dir(tmp_path))
        engine._recognizer = _FakeRecognizer({}, text="テストです")  # type: ignore[assignment]
        with pytest.raises(ValueError):
            engine.transcribe(np.zeros(8000, dtype=np.int16), 8000)


class TestThreads:
    def test_zero_threads_folds_to_a_usable_default(self, tmp_path: Path) -> None:
        """`asr.cpu_threads: 0` は「ライブラリ既定」の規約。sherpa には 0 を渡せない。"""
        engine = SherpaOnnxEngine(make_model_dir(tmp_path), num_threads=0)
        assert engine._num_threads > 0


class TestConfig:
    def test_defaults_match_the_measured_winner(self) -> None:
        """既定値が #28 の実測結果からずれたら気付けるようにしておく。"""
        sherpa = AsrConfig().sherpa
        assert sherpa.model == "reazonspeech-k2-v2"
        assert sherpa.lead_in_ms == 600  # 0 だと CER が 19.70% に跳ねる
        assert sherpa.decoding_method == "modified_beam_search"

    def test_negative_lead_in_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            SherpaConfig(lead_in_ms=-1)

    def test_hotwords_file_resolves_against_the_repo_root(self) -> None:
        resolved = SherpaConfig(hotwords_file="tests/fixtures/ja_hotwords.txt").resolved_hotwords_file()
        assert resolved is not None and resolved.is_absolute()

    def test_no_hotwords_file_means_none(self) -> None:
        assert SherpaConfig().resolved_hotwords_file() is None


class _FakeRecognizer:
    """sherpa_onnx.OfflineRecognizer の最小スタブ（渡された音声を記録する）。"""

    def __init__(self, seen: dict, text: str) -> None:
        self._seen = seen
        self._text = text

    def create_stream(self) -> "_FakeStream":
        return _FakeStream(self._seen, self._text)

    def decode_stream(self, stream: "_FakeStream") -> None:
        stream.decoded = True


class _FakeStream:
    def __init__(self, seen: dict, text: str) -> None:
        self._seen = seen
        self.decoded = False
        self.result = type("R", (), {"text": text})()

    def accept_waveform(self, sample_rate: int, audio: np.ndarray) -> None:
        self._seen["sample_rate"] = sample_rate
        self._seen["audio"] = audio
