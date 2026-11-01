"""NLLB-200 distilled 600M（CTranslate2 int8）による TranslationEngine 実装（イシュー#12）。

速度重視のエンジン。ライセンスは CC-BY-NC 4.0（非商用）— 学校の授業利用は
非商用の想定（plan.md A-05 / R-05）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from server.mt.base import TranslationEngine
from server.model_files import TRANSLATION_GUIDANCE, TranslationModelError

logger = logging.getLogger("server.mt")

# config の言語コード → NLLB(FLORES-200) 言語コード
NLLB_LANG_CODES: dict[str, str] = {
    "en": "eng_Latn",
    "zh": "zho_Hans",  # 簡体字
}
SOURCE_LANG = "jpn_Jpan"


class NllbEngine(TranslationEngine):
    def __init__(self, model_dir: Path, tokenizer_dir: Path, beam_size: int = 1) -> None:
        for path in (model_dir, tokenizer_dir):
            if not path.exists():
                raise FileNotFoundError(
                    f"翻訳モデルが見つからない: models\n{TRANSLATION_GUIDANCE}"
                )
        self._model_dir = model_dir
        self._tokenizer_dir = tokenizer_dir
        self._beam_size = beam_size
        self._translator: Any = None
        self._tokenizer: Any = None

    @property
    def model_version(self) -> str:
        """モデルディレクトリ名＋ビーム幅。ビーム幅が変われば訳文も変わるので
        翻訳キャッシュ（#26）のキー空間を分ける。"""
        return f"{self._model_dir.name}@beam{self._beam_size}"

    def warmup(self) -> None:
        import ctranslate2
        from transformers import AutoTokenizer

        if self._translator is not None:
            return  # 冪等: 背後warmupと遅延ロードの二重ロードを防ぐ
        logger.info("翻訳モデルを読み込み中")
        try:
            translator = ctranslate2.Translator(str(self._model_dir), device="cpu")
        except RuntimeError as exc:
            if not str(exc).startswith(("Unsupported model binary version", "Unable to open file")):
                raise
            raise TranslationModelError(
                f"翻訳モデルの形式が不正です: models\n{TRANSLATION_GUIDANCE}"
            ) from None
        try:
            tokenizer = AutoTokenizer.from_pretrained(
                str(self._tokenizer_dir), src_lang=SOURCE_LANG
            )
        except (OSError, ValueError) as exc:
            if not str(exc).startswith(("Can't load tokenizer for", "Unrecognized model in")):
                raise
            raise TranslationModelError(
                f"翻訳モデルの形式が不正です: models\n{TRANSLATION_GUIDANCE}"
            ) from None
        self._translator = translator
        self._tokenizer = tokenizer
        self.translate("こんにちは。", "en")  # ダミー推論（初回遅延対策）
        logger.info("翻訳モデルの準備完了")

    def translate(self, text_ja: str, target_lang: str) -> str:
        code = NLLB_LANG_CODES.get(target_lang)
        if code is None:
            raise ValueError(f"翻訳モデルが未対応の言語: {target_lang}")
        if self._translator is None:  # 通常は起動時 warmup 済み。直接利用時の保険
            self.warmup()
        tokens = self._tokenizer.convert_ids_to_tokens(self._tokenizer(text_ja).input_ids)
        results = self._translator.translate_batch(
            [tokens], target_prefix=[[code]], beam_size=self._beam_size
        )
        output_tokens = results[0].hypotheses[0][1:]  # 先頭の言語トークンを除去
        text: str = self._tokenizer.decode(
            self._tokenizer.convert_tokens_to_ids(output_tokens), skip_special_tokens=True
        )
        return text.strip()

    def supported_languages(self) -> list[str]:
        return list(NLLB_LANG_CODES)
