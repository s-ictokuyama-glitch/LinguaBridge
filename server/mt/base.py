"""TranslationEngine 抽象（plan.md §6.5）。テストと実装の合意済みシーム。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TranslationEngine(ABC):
    @abstractmethod
    def translate(self, text_ja: str, target_lang: str) -> str:
        """日本語1発話を target_lang へ翻訳する。ワーカースレッドで呼ばれる。"""

    @abstractmethod
    def supported_languages(self) -> list[str]: ...

    def warmup(self) -> None:
        """起動時ロード＆ダミー推論（初回遅延対策）。フェイクでは何もしない。"""
