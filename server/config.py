"""config.yaml のロードと検証（plan.md §5 のスキーマ骨子）。"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


class ServerConfig(BaseModel):
    http_port: int = 8000
    https_port: int = 8443
    cert_dir: str = "certs/"


class AsrConfig(BaseModel):
    engine: str = "fake"  # "fake" | "faster-whisper"（#11で追加）
    model: str = "kotoba-tech/kotoba-whisper-v2.0-faster"
    compute_type: str = "int8"
    language: str = "ja"


class VadConfig(BaseModel):
    threshold: float = 300
    min_silence_ms: int = 500
    max_utterance_s: int = 30


class HyMt2Config(BaseModel):
    gguf_path: str = "models/hy-mt2-1.8b-q4.gguf"
    threads: int = 4


class NllbConfig(BaseModel):
    model_dir: str = "models/nllb-200-600m-ct2"
    beam_size: int = 1


class MtConfig(BaseModel):
    engine: str = "fake"  # "fake" | "hy-mt2" | "nllb"（#12で追加、既定は#9ベンチで確定）
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
