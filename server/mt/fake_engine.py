"""決定的フェイク翻訳（#10 トレーサー用）。

日本語 → 言語マーカー付きの既知訳文: "[en] <原文>" の形式。
calls に (text_ja, target_lang) を記録するので、テストは
「選択者0名の言語に翻訳ジョブが発生しない」ことを検査できる。
"""

from __future__ import annotations

from server.mt.base import TranslationEngine


class FakeTranslationEngine(TranslationEngine):
    def __init__(self, languages: list[str] | None = None) -> None:
        self._languages = languages if languages is not None else ["en", "zh"]
        self.calls: list[tuple[str, str]] = []  # テストからの検査用

    def translate(self, text_ja: str, target_lang: str) -> str:
        self.calls.append((text_ja, target_lang))
        return f"[{target_lang}] {text_ja}"

    def supported_languages(self) -> list[str]:
        return list(self._languages)
