"""Hy-MT2-1.8B（llama.cpp / GGUF int4）による TranslationEngine 実装（イシュー#12）。

品質重視のエンジンで判断ゲート①の既定（docs/bench/2026-07-07-bench.md）。
配布元は tencent/Hy-MT2-1.8B-GGUF（Apache-2.0）。プロンプト形式と
サンプリングパラメータはモデルカードの推奨値に従う。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from server.mt.base import TranslationEngine

logger = logging.getLogger(__name__)

# config の言語コード → プロンプトに書く言語名（モデルカードは英語名を指定）
HYMT_LANG_LABELS: dict[str, str] = {
    "en": "English",
    "zh": "Simplified Chinese",
}

# モデルカードの推奨サンプリングパラメータ（temperature のみ config で調整可）
TOP_P = 0.6
TOP_K = 20
REPEAT_PENALTY = 1.05
MAX_TOKENS = 512


def build_prompt(text_ja: str, lang_label: str) -> str:
    """モデルカード記載の翻訳指示プロンプト。"""
    return (
        f"Translate the following text into {lang_label}. Note that you should "
        f"only output the translated result without any additional explanation: {text_ja}"
    )


class HyMt2Engine(TranslationEngine):
    def __init__(self, gguf_path: Path, threads: int = 4, temperature: float = 0.7) -> None:
        if not gguf_path.exists():
            raise FileNotFoundError(
                f"Hy-MT2のGGUFが見つからない: {gguf_path}\n"
                "scripts/download_models.py を実行してモデルを取得してください"
            )
        self._gguf_path = gguf_path
        self._threads = threads
        self._temperature = temperature
        self._llm: Any = None

    def warmup(self) -> None:
        from llama_cpp import Llama

        logger.info("Hy-MT2モデルをロード中: %s", self._gguf_path.name)
        llm = Llama(
            model_path=str(self._gguf_path),
            n_ctx=2048,
            n_threads=self._threads,
            verbose=False,
        )
        # チャットテンプレートがGGUFに無いと llama.cpp が別形式に暗黙フォールバックし
        # 出力が壊れる。公式GGUFには埋め込み済み — 無ければ入手元を疑う
        if "tokenizer.chat_template" not in (llm.metadata or {}):
            raise RuntimeError(
                f"GGUFにチャットテンプレートが無い: {self._gguf_path}\n"
                "tencent/Hy-MT2-1.8B-GGUF の公式ファイルか確認してください"
            )
        self._llm = llm
        self.translate("こんにちは。", "en")  # ダミー推論（初回遅延対策）
        logger.info("Hy-MT2ウォームアップ完了")

    def translate(self, text_ja: str, target_lang: str) -> str:
        label = HYMT_LANG_LABELS.get(target_lang)
        if label is None:
            raise ValueError(f"Hy-MT2エンジン未対応の言語: {target_lang}")
        if self._llm is None:  # 通常は起動時 warmup 済み。直接利用時の保険
            self.warmup()
        response = self._llm.create_chat_completion(
            messages=[{"role": "user", "content": build_prompt(text_ja, label)}],
            temperature=self._temperature,
            top_p=TOP_P,
            top_k=TOP_K,
            repeat_penalty=REPEAT_PENALTY,
            max_tokens=MAX_TOKENS,
        )
        return str(response["choices"][0]["message"]["content"]).strip()

    def supported_languages(self) -> list[str]:
        return list(HYMT_LANG_LABELS)
