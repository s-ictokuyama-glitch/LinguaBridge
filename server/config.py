"""config.yaml のロードと検証（plan.md §5 のスキーマ骨子）。"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# リポジトリルート（config.py は server/ 配下）。相対パスの証明書を cwd に依存せず解決する
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _under_root(p: Path) -> Path:
    return p if p.is_absolute() else _PROJECT_ROOT / p


class ServerConfig(BaseModel):
    http_port: int = 8000
    https_port: int = 8443
    cert_dir: str = "certs/"
    cert_file: str = "cert.pem"  # cert_dir 配下
    key_file: str = "key.pem"

    def cert_path(self) -> Path:
        # cwd 非依存: 相対 cert_dir はリポジトリルート基準で解決する
        return _under_root(Path(self.cert_dir) / self.cert_file)

    def key_path(self) -> Path:
        return _under_root(Path(self.cert_dir) / self.key_file)

    def tls_ready(self) -> bool:
        return self.cert_path().exists() and self.key_path().exists()


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


class SherpaConfig(BaseModel):
    """sherpa-onnx + ReazonSpeech K2 v2（#28）。`asr.engine: sherpa` のときだけ効く。"""

    model: str = "reazonspeech-k2-v2"  # models.dir 配下のディレクトリ名
    # 発話の先頭に足す無音の長さ。#28 の実測で精度を支配する要因だった
    # （0ms で CER 19.70% / 600ms で 3.48%。600ms 付近からプラトー）。
    # 上流の vad.pre_roll_ms(240) では足りない。詳細は server/asr/sherpa_engine.py
    lead_in_ms: int = 600
    # 既定 beam は #28 の実測（CER 3.48%→2.09%、decode 中央値は 0.11s→0.13s しか増えない）。
    # hotwords（教科用語の contextual biasing）も modified_beam_search でしか効かない
    decoding_method: str = "modified_beam_search"  # "greedy_search" | "modified_beam_search"
    hotwords_file: str | None = None  # リポジトリルートからの相対パス。None で無効
    hotwords_score: float = 1.5

    @field_validator("lead_in_ms")
    @classmethod
    def _lead_in_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("asr.sherpa.lead_in_ms は0以上にすること")
        return v

    def resolved_hotwords_file(self) -> Path | None:
        if not self.hotwords_file:
            return None
        return _under_root(Path(self.hotwords_file))


class AsrConfig(BaseModel):
    engine: str = "faster-whisper"  # "faster-whisper" | "sherpa" | "fake"（テスト・デモ用）
    # models.dir 配下のディレクトリ名。既定はベンチ確定値（docs/bench/2026-07-07-bench.md）
    model: str = "faster-whisper-small"
    compute_type: str = "int8"
    language: str = "ja"
    # CTranslate2 の内部スレッド数（#26 B-6）。0 = ライブラリ既定（全論理コア）。
    # 開発機の実測では組合せを変えても合計の遅延は動かなかった（空いたCPUを llama.cpp が
    # 食うため）ので既定は 0。ASR単体の尾を縮めたいときだけ 6〜8 にする。
    # docs/bench/2026-08-23-hymt-tuning.md
    cpu_threads: int = 0

    @field_validator("cpu_threads")
    @classmethod
    def _threads_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("asr.cpu_threads は0以上にすること（0 = ライブラリ既定）")
        return v

    sherpa: SherpaConfig = Field(default_factory=SherpaConfig)


class VadConfig(BaseModel):
    engine: str = "silero"  # "silero" | "energy"（energyはテスト・フォールバック用）
    threshold: float = 0.5  # silero: 音声確率 0..1 / energy: int16 RMS
    min_silence_ms: int = 500
    max_utterance_s: int = 30
    pre_roll_ms: int = 240  # 発話開始前の音声を含める長さ（語頭の欠け防止）


class TurnMorphConfig(BaseModel):
    """morph 戦略のセグメンテーション定数（#27）。

    **VAD の分割パラメータをここに閉じ込めてあるのは意図的**。strategy=morph を
    選んだときだけ、この3つがまとめて有効になる（片方だけ入った設定を書けない）。

    既定値は Parapper の実測既定（320 / 96 / 640）ではなく、
    **この音源・この ASR での実測の勝者**（docs/bench/2026-08-23-turn-detection.md）。
    320ms へ下げると Segment が 22→35 に増え、ASR は固定費が支配的（#24）なので
    E2E の CPU 中央値が 266%→358% に上がるうえ、断片が短くなったぶん誤認識由来の
    分割が1件増えた。**文中で切らない効果は無音長ではなく文法境界が出している。**
    """

    # Segment を確定する無音長。vad.min_silence_ms の代わりに使われる。
    # 現行と同じ 500ms のままにして ASR 呼び出し回数を増やさない（上のコメント参照）
    check_silence_ms: int = 500
    # 発話開始に必要な連続音声時間。現在は32ms相当＝VAD誤検知1枚で発話が立ち上がる
    # （Parapper B-3）。合成音源では効果を確認できなかったが、実マイクの誤検知除け
    # として残す。効果の確認は実地検証（学校実機）に回す
    start_speech_ms: int = 96
    # 文法が「継続」と言っても無条件に Turn を確定する無音長。
    # 640ms だと、SAPI が読点に入れる 640ms の間（rate-slow-b）とちょうど衝突して
    # 文中で割れた。継続クラスのときしか効かない閾値なので、上げても
    # StrongEnd/PredicateEnd の endpoint latency は伸びない
    force_silence_ms: int = 1000
    max_turn_s: float = 30.0  # 1 Turn の音声長の上限（連結の暴走防止）
    max_segments: int = 12  # 1 Turn に連結する Segment 数の上限

    @model_validator(mode="after")
    def _force_not_shorter_than_check(self) -> TurnMorphConfig:
        if self.force_silence_ms < self.check_silence_ms:
            raise ValueError(
                "turn.morph.force_silence_ms は check_silence_ms 以上にすること"
                "（Segment より先に Turn が確定してしまう）"
            )
        return self


class PartialConfig(BaseModel):
    """partial 字幕（先生のみ）と生徒の「発話中」インジケーター（#29）。

    **2つは独立した設定**にしてある。インジケーターは VAD 由来で ASR を1回も
    増やさないので、partial が実測で割に合わなくても単独で残せる。

    partial の既定が false なのはチケットの合意（「CPUコストと UX 改善の両方を実測し、
    割に合わなければ既定OFFで出荷する」）。判定は docs/bench/2026-08-24-partial.md。
    """

    enabled: bool = False
    # 発話の**途中**でこの長さの無音に達したら interim ASR を発火する（Parapper 方式）。
    # タイマー駆動ではなく息継ぎ駆動なので、partial の回数が発話のリズムに比例し
    # CPU 予算が読める。vad.min_silence_ms / turn.morph.check_silence_ms より
    # フレーム単位で短いこと（VoiceSegmenter が検証する）
    interim_silence_ms: int = 96
    # これ未満の音声では interim を出さない。#28 の実測でリードインが精度を支配しており
    # （0ms で CER 19.70% / 600ms で 3.48%）、短すぎる音声の partial は先生に
    # 誤った日本語を見せるだけになる
    min_interim_audio_s: float = 0.6
    # 生徒の「発話中」インジケーター。ASR を1回も増やさないので既定 ON
    speaking_indicator: bool = True

    @field_validator("interim_silence_ms")
    @classmethod
    def _interim_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("partial.interim_silence_ms は1以上にすること")
        return v

    @field_validator("min_interim_audio_s")
    @classmethod
    def _min_audio_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("partial.min_interim_audio_s は0以上にすること")
        return v


class TurnConfig(BaseModel):
    """Segment を Turn（字幕カード1枚）へ束ねる戦略（#27）。

    既定は morph。docs/bench/2026-08-23-turn-detection.md の実測で、
    **不自然な分割 3件→0件・Hy-MT2 呼び出し 44→38回・ASR 呼び出しは据え置き**
    （E2E の遅延と CPU は周回のばらつきの内）だったため、#27 の合意どおり既定を変えた。
    `simple` は #27 以前と完全に同一の挙動で、この1行を戻せば現行へ戻せる。
    #31 の namo はここに3段目として増える想定。

    **`surface` は句読点だけを見ているのではなく用言の終止形も見ている**。#28 で
    ReazonSpeech K2（句読点を出さない）へ替えたとき、日本語の敬体が `〜です`/`〜ます` で
    終わるぶん `predicate_end`(15回) が `strong_end`(0回) を丸ごと代替し、
    区切りは whisper 版と1件も違わなかった。形態素解析器は不要のまま。
    """

    strategy: str = "morph"  # "simple" | "morph"
    classifier: str = "surface"  # 文法境界の判定器。形態素解析器版はここに増える
    morph: TurnMorphConfig = Field(default_factory=TurnMorphConfig)

    @field_validator("strategy")
    @classmethod
    def _known_strategy(cls, v: str) -> str:
        if v not in ("simple", "morph"):
            raise ValueError(f"turn.strategy は simple / morph のいずれか（指定値: {v}）")
        return v

    def segmenter_kwargs(self, vad: VadConfig) -> dict:
        """VoiceSegmenter に渡す分割パラメータ。戦略でまとめて切り替わる。"""
        if self.strategy == "simple":
            return {
                "min_silence_ms": vad.min_silence_ms,
                "start_speech_ms": 0,
                "force_silence_ms": None,
            }
        return {
            "min_silence_ms": self.morph.check_silence_ms,
            "start_speech_ms": self.morph.start_speech_ms,
            "force_silence_ms": self.morph.force_silence_ms,
        }


class HyMt2Config(BaseModel):
    gguf_path: str = "hy-mt2/Hy-MT2-1.8B-Q4_K_M.gguf"  # models.dir からの相対
    threads: int = 4
    # 0 = 貪欲デコード。モデルカードの推奨は 0.7 だが、#26 B-5 の実測（品質差なし・
    # 遅延差なし・出力が安定）で貪欲を既定にした。docs/bench/2026-08-23-hymt-tuning.md
    temperature: float = 0.0
    # 1発話あたりの出力トークン上限の頭打ち（#26 B-7）。実際の上限は入力長から算出され、
    # この値で頭を押さえる。壊れた入力での暴走生成を有界にするための保険
    max_tokens_cap: int = 512


class NllbConfig(BaseModel):
    model_dir: str = "nllb-200-distilled-600M-ct2"  # models.dir からの相対
    tokenizer_dir: str = "nllb-tokenizer"
    beam_size: int = 1


class MtConfig(BaseModel):
    # 既定は判断ゲート①の確定値（docs/bench/2026-07-07-bench.md）
    engine: str = "hy-mt2"  # "hy-mt2" | "nllb" | "fake"（テスト・デモ用）
    hy_mt2: HyMt2Config = Field(default_factory=HyMt2Config)
    nllb: NllbConfig = Field(default_factory=NllbConfig)
    # 発話をまたぐ翻訳キャッシュの上限件数（#26 B-4）。0 = 無効。
    # **必ず有界**にする（45分授業でのメモリ単調増加を作らない）
    cache_size: int = 512

    @field_validator("cache_size")
    @classmethod
    def _cache_size_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("mt.cache_size は0以上にすること（0 = 無効）")
        return v


class Language(BaseModel):
    code: str
    label: str


class MonitoringConfig(BaseModel):
    stats_interval_s: float = 2.0  # 先生ページへの統計配信間隔
    silence_warning_s: float = 30.0  # 配信中の無音警告までの秒数（E-01）
    overload_queue_depth: int = 3  # 翻訳キューの件数による過負荷判定（E-05, plan.md §6.3）
    # ASR側の過負荷は件数でなく秒数で判定する（#25 A-5）。1発話は 0.3〜30秒と幅があり、
    # 「3件詰まっている」が 1秒ぶんなのか 30秒ぶんなのか件数からは分からないため。
    # 値の根拠: #24 のベースライン E2E は健全な状態でも p95 5.44s / 最大 8.1s まで振れる
    # （処理中のセグメントを含む指標なので、1発話が長いだけで数秒になる）。
    # 5秒だと健全な授業で警告が出てしまうため、健全時の最大より上に置く
    overload_audio_seconds: float = 10.0


class LimitsConfig(BaseModel):
    """過負荷・不正入力に対する境界（#25）。

    既定値は「授業では踏まないが、壊れ方が壊滅的にならない」水準に置く。
    どれも上限に達したときの振る舞いが定義されていることが本質で、
    値そのものは現場で調整してよい。
    """

    # ASR待ち音声の上限（秒）。件数でなく秒数で持つ（Parapper R-6）。
    # 超過分は捨てるが、先生へ通知する（黙って捨てない）
    asr_queue_seconds: float = 30.0
    mt_queue_max: int = 256  # 翻訳キューの件数上限（無制限にしない）
    # ここを超えたら再接続復元ジョブの投入を止める。溢れそうなとき、
    # 先に抑制されるのはライブ字幕ではなく復元側（生徒は再接続でやり直せる）
    mt_replay_watermark: int = 128
    send_queue_max: int = 64  # クライアント1人あたりの送信キュー件数
    send_timeout_s: float = 5.0  # 1メッセージの送信タイムアウト
    max_students: int = 64  # 同時接続する生徒の上限（whisper-flow W-4）
    max_text_bytes: int = 8192  # テキストフレームの受信上限
    max_audio_bytes: int = 32000  # バイナリフレームの受信上限（1秒 @16kHz PCM16）
    asr_timeout_s: float = 60.0  # 1発話のASR推論タイムアウト（whisper-flow W-1）
    mt_timeout_s: float = 60.0  # 1件の翻訳のタイムアウト
    # WSの Origin 許可リスト。空なら「Hostと同一ホストのみ許可」。
    # Origin ヘッダを持たない接続（非ブラウザのツール類）は常に許可する
    allowed_origins: list[str] = Field(default_factory=list)

    @field_validator("mt_replay_watermark")
    @classmethod
    def _watermark_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("mt_replay_watermark は1以上にすること")
        return v


class RecordingConfig(BaseModel):
    default_on: bool = False
    out_dir: str = "sessions/"

    @property
    def resolved_out_dir(self) -> Path:
        # cwd 非依存: 相対 out_dir はリポジトリルート基準で解決する
        return _under_root(Path(self.out_dir))


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    vad: VadConfig = Field(default_factory=VadConfig)
    turn: TurnConfig = Field(default_factory=TurnConfig)
    partial: PartialConfig = Field(default_factory=PartialConfig)
    mt: MtConfig = Field(default_factory=MtConfig)
    languages: list[Language] = Field(
        default_factory=lambda: [
            Language(code="en", label="English"),
            Language(code="zh", label="中文（简体）"),
        ]
    )
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    history_resend: int = 50

    @model_validator(mode="after")
    def _replay_watermark_fits_queue(self) -> AppConfig:
        if self.limits.mt_replay_watermark > self.limits.mt_queue_max:
            raise ValueError("limits.mt_replay_watermark は mt_queue_max 以下にすること")
        return self

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
