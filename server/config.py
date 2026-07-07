"""config.yaml のロードと検証（plan.md §5 のスキーマ骨子）。"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


class ServerConfig(BaseModel):
    http_port: int = 8000
    https_port: int = 8443
    cert_dir: str = "certs/"


class ModelsConfig(BaseModel):
    """モデル格納先。OneDrive同期の影響を受けない場所に置く（plan.md R-08）。"""

    dir: str = "%LOCALAPPDATA%/LinguaBridge/models"

    @property
    def resolved_dir(self) -> Path:
        expanded = os.path.expandvars(os.path.expanduser(self.dir))
        if "%" in expanded:
            raise ValueError(f"models.dir の環境変数が解決できない: {self.dir}")
        return Path(expanded)

    def resolve(self, relative: str) -> Path:
        """models.dir からの相対パス（gguf_path / model_dir 等）を絶対パスにする。"""
        return self.resolved_dir / relative


class AsrConfig(BaseModel):
    engine: str = "faster-whisper"  # "faster-whisper" | "fake"（テスト・デモ用）
    # models.dir 配下のディレクトリ名。既定はベンチ確定値（docs/bench/2026-07-07-bench.md）
    model: str = "faster-whisper-small"
    compute_type: str = "int8"
    language: str = "ja"


class VadConfig(BaseModel):
    engine: str = "silero"  # "silero" | "energy"（energyはテスト・フォールバック用）
    threshold: float = 0.5  # silero: 音声確率 0..1 / energy: int16 RMS
    min_silence_ms: int = 500
    max_utterance_s: int = 30
    pre_roll_ms: int = 240  # 発話開始前の音声を含める長さ（語頭の欠け防止）


class HyMt2Config(BaseModel):
    gguf_path: str = "hy-mt2/Hy-MT2-1.8B-Q4_K_M.gguf"  # models.dir からの相対
    threads: int = 4
    temperature: float = 0.7  # モデルカード推奨値。テスト等では 0（貪欲）で決定的にできる


class NllbConfig(BaseModel):
    model_dir: str = "nllb-200-distilled-600M-ct2"  # models.dir からの相対
    tokenizer_dir: str = "nllb-tokenizer"
    beam_size: int = 1


class MtConfig(BaseModel):
    # 既定は判断ゲート①の確定値（docs/bench/2026-07-07-bench.md）
    engine: str = "hy-mt2"  # "hy-mt2" | "nllb" | "fake"（テスト・デモ用）
    hy_mt2: HyMt2Config = Field(default_factory=HyMt2Config)
    nllb: NllbConfig = Field(default_factory=NllbConfig)


class Language(BaseModel):
    code: str
    label: str


class RecordingConfig(BaseModel):
    default_on: bool = False
    out_dir: str = "sessions/"


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    vad: VadConfig = Field(default_factory=VadConfig)
    mt: MtConfig = Field(default_factory=MtConfig)
    languages: list[Language] = Field(
        default_factory=lambda: [
            Language(code="en", label="English"),
            Language(code="zh", label="中文（简体）"),
        ]
    )
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    history_resend: int = 50

    @field_validator("languages")
    @classmethod
    def _languages_not_empty(cls, v: list[Language]) -> list[Language]:
        if not v:
            raise ValueError("languages must not be empty")
        codes = [lang.code for lang in v]
        if len(codes) != len(set(codes)):
            raise ValueError("language codes must be unique")
        return v

    @property
    def language_codes(self) -> list[str]:
        return [lang.code for lang in self.languages]


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(data)
