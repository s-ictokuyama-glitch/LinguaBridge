"""Hy-MT2-1.8B（llama.cpp / GGUF int4）による TranslationEngine 実装（イシュー#12）。

品質重視のエンジンで判断ゲート①の既定（docs/bench/2026-07-07-bench.md）。
配布元は tencent/Hy-MT2-1.8B-GGUF（Apache-2.0）。プロンプト形式はモデルカードに従う。

デコードは **貪欲（temperature=0）が既定**（#26 B-5 の実測で決定 —
docs/bench/2026-08-23-hymt-tuning.md）。モデルカードの推奨はサンプリングだが、
往復翻訳での品質差は測定できず遅延も変わらない一方、同一入力で同じ訳が出る割合が
100%（サンプリングは55%）になる。翻訳キャッシュ（#26 B-4）は最初に出た訳を固定するので、
固定するなら乱数の1サンプルより argmax の方がよい。

**ただし決定性は保証ではない**。同一入力の連続呼び出しでは安定するが、直前の呼び出しが
変わると出力が変わる例を観測している（llama.cpp はプロンプトの共通接頭辞を KV キャッシュ
から再利用する）。**訳文を文字列一致で検査するテストは書かないこと**。
`config.yaml` の `mt.hy_mt2.temperature` を 0 より大きくすればサンプリングに戻せる。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from server.mt.base import TranslationEngine

logger = logging.getLogger(__name__)

# config の言語コード → プロンプトに書く言語名（モデルカードは英語名を指定）
HYMT_LANG_LABELS: dict[str, str] = {
    "en": "English",
    "zh": "Simplified Chinese",
}

# サンプリング時のみ効くパラメータ（モデルカード推奨値）。貪欲では無視される
TOP_P = 0.6
TOP_K = 20
REPEAT_PENALTY = 1.05

# 出力上限の算出（#26 B-7）。翻訳の出力長は入力長にほぼ比例するので、
# 「入力トークン数 × 係数 + 余白」で頭を押さえる。ASR が壊れた入力を出したときの
# 暴走生成が、入力の長さに見合った時間で必ず止まるようにするのが目的。
# 係数は日本語→英語の膨張（1トークンあたり概ね1〜1.5トークン）に余裕を見た値
OUTPUT_TOKEN_RATIO = 2.0
OUTPUT_TOKEN_MARGIN = 24
MIN_MAX_TOKENS = 32  # 「はい」のような極端に短い入力でも訳文が切れないための下限


def build_prompt(text_ja: str, lang_label: str) -> str:
    """モデルカード記載の翻訳指示プロンプト。"""
    return (
        f"Translate the following text into {lang_label}. Note that you should "
        f"only output the translated result without any additional explanation: {text_ja}"
    )


def max_tokens_for(input_tokens: int, cap: int) -> int:
    """入力トークン数から出力上限を決める（#26 B-7）。

    固定の 512 だと、短い発話でも数百トークンぶんの暴走生成を許してしまう
    （1トークンあたり数十msなので、そのまま遅延になる）。
    """
    if cap < MIN_MAX_TOKENS:
        raise ValueError(f"max_tokens_cap は {MIN_MAX_TOKENS} 以上にすること（got {cap}）")
    estimated = math.ceil(max(0, input_tokens) * OUTPUT_TOKEN_RATIO) + OUTPUT_TOKEN_MARGIN
    return max(MIN_MAX_TOKENS, min(cap, estimated))


class HyMt2Engine(TranslationEngine):
    def __init__(
        self,
        gguf_path: Path,
        threads: int = 4,
        temperature: float = 0.0,
        max_tokens_cap: int = 512,
    ) -> None:
        if not gguf_path.exists():
            raise FileNotFoundError(
                f"Hy-MT2のGGUFが見つからない: {gguf_path}\n"
                "scripts/download_models.py を実行してモデルを取得してください"
            )
        self._gguf_path = gguf_path
        self._threads = threads
        self._temperature = temperature
        self._max_tokens_cap = max_tokens_cap
        self._llm: Any = None

    @property
    def model_version(self) -> str:
        """モデルファイル名＋デコード設定。デコードが変われば出力も変わるので、
        翻訳キャッシュ（#26）のキー空間を分ける。"""
        decoding = "greedy" if self._temperature <= 0 else f"t{self._temperature:g}"
        return f"{self._gguf_path.name}@{decoding}"

    def warmup(self) -> None:
        from llama_cpp import Llama

        if self._llm is not None:
            return  # 冪等: 背後warmupと遅延ロードの二重ロードを防ぐ
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

    def _count_input_tokens(self, text_ja: str) -> int:
        """原文のトークン数。プロンプトの定型部分は毎回同じなので数えない。"""
        try:
            return len(self._llm.tokenize(text_ja.encode("utf-8"), add_bos=False, special=False))
        except Exception:  # トークナイザAPIの差異で落ちても翻訳自体は続ける
            logger.debug("トークン数の取得に失敗。文字数で代用します", exc_info=True)
            return len(text_ja)

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
            max_tokens=max_tokens_for(self._count_input_tokens(text_ja), self._max_tokens_cap),
        )
        return str(response["choices"][0]["message"]["content"]).strip()

    def supported_languages(self) -> list[str]:
        return list(HYMT_LANG_LABELS)
