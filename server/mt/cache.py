"""発話をまたぐ翻訳キャッシュ（イシュー#26 B-4）。

`Utterance.translations` は1発話ぶんのメモ化しかしないので、「はい」「そうですね」
「もう一度言います」のような授業中の繰り返しが毎回 Hy-MT2 を叩く（1回 570〜850ms）。
ここは**発話をまたいで**同じ原文の訳を使い回す。

**必ず bounded**。45分の授業でメモリが単調増加すると #32 の長時間試験で落ちる。
上限に達したら最も長く使われていないものから捨てる（LRU）。

キーにエンジン名とモデル版を含めるのは、設定を変えたときに古い訳を出さないため。
`model_version` にはデコード設定（貪欲/サンプリング）も入る — 同じモデルでも
デコードが変われば出力が変わるので、別のキー空間として扱う。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class CacheKey:
    """原文と、その訳文を一意に決めるものすべて。

    `source_lang` は現状 "ja" 固定だが、キーに含めておく（先生の言語が
    変わる将来にキー衝突で気づけなくなるのを防ぐ）。
    """

    source_text: str
    source_lang: str
    target_lang: str
    engine: str
    model_version: str


@dataclass(frozen=True)
class CacheStats:
    hits: int
    misses: int
    size: int
    maxsize: int

    @property
    def hit_rate(self) -> float:
        """参照のうちヒットした割合。参照0回なら 0.0。"""
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class TranslationCache:
    """有界 LRU の翻訳キャッシュ。

    `maxsize=0` で無効化（`get` は常に None、`put` は何もしない）。設定で切れる形に
    しておくのは、キャッシュを疑うときに再ビルドせず切り分けられるようにするため。
    """

    def __init__(self, maxsize: int) -> None:
        if maxsize < 0:
            raise ValueError("maxsize は0以上にすること（0 = 無効）")
        self._maxsize = maxsize
        self._entries: OrderedDict[CacheKey, str] = OrderedDict()
        # 参照は現状イベントループの単一スレッドからだが、将来ワーカー側から
        # 触られても壊れないようにしておく（取りこぼしより競合の方が見つけにくい）
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @property
    def enabled(self) -> bool:
        return self._maxsize > 0

    def get(self, key: CacheKey) -> str | None:
        if not self.enabled:
            return None
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                self._misses += 1
                return None
            self._entries.move_to_end(key)  # 直近使用として末尾へ
            self._hits += 1
            return value

    def put(self, key: CacheKey, value: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)  # 最も長く使われていないものを捨てる

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(
                hits=self._hits,
                misses=self._misses,
                size=len(self._entries),
                maxsize=self._maxsize,
            )

    def clear(self) -> None:
        """内容と統計を捨てる。セッション間で数値を持ち越さないため。"""
        with self._lock:
            self._entries.clear()
            self._hits = 0
            self._misses = 0
