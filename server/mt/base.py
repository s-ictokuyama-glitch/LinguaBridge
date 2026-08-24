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

    @property
    def model_version(self) -> str:
        """同じ原文に同じ訳文を返す条件を表す識別子（#26 の翻訳キャッシュのキー）。

        モデルファイルだけでなく**出力を変える設定**（デコード方式・ビーム幅など）も
        含めること。含め忘れると、設定を変えたあとに古い設定で作られた訳文が
        キャッシュから返り続ける。実装は必ず上書きすること（既定値は保険）。
        """
        return "unknown"
