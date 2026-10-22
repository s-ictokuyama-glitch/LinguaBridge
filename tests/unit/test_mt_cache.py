"""発話をまたぐ翻訳キャッシュの単体テスト（イシュー#26 B-4）。

守りたいのは3つ。
  1. **有界**であること（45分授業でメモリが単調増加しない）
  2. キーが**混ざらない**こと（言語・エンジン・モデル版が違えば別の訳）
  3. ヒット率が**参照の実績**を表すこと（stats に出す値の根拠）
"""

from __future__ import annotations

import pytest

from server.mt.cache import CacheKey, TranslationCache


def key(text: str, lang: str = "en", engine: str = "hy-mt2", version: str = "v1") -> CacheKey:
    return CacheKey(
        source_text=text,
        source_lang="ja",
        target_lang=lang,
        engine=engine,
        model_version=version,
    )


class TestBounded:
    def test_never_grows_past_maxsize(self):
        cache = TranslationCache(maxsize=3)
        for i in range(100):
            cache.put(key(f"発話{i}"), f"utterance {i}")
        assert cache.stats().size == 3

    def test_evicts_least_recently_used(self):
        cache = TranslationCache(maxsize=2)
        cache.put(key("あ"), "A")
        cache.put(key("い"), "B")
        assert cache.get(key("あ")) == "A"  # 「あ」を直近使用にする
        cache.put(key("う"), "C")  # 溢れる: 捨てられるのは「い」
        assert cache.get(key("あ")) == "A"
        assert cache.get(key("い")) is None
        assert cache.get(key("う")) == "C"

    def test_reinserting_same_key_does_not_grow(self):
        cache = TranslationCache(maxsize=2)
        for _ in range(10):
            cache.put(key("はい"), "Yes")
        assert cache.stats().size == 1

    def test_negative_maxsize_is_rejected(self):
        with pytest.raises(ValueError):
            TranslationCache(maxsize=-1)


class TestDisabled:
    """`maxsize=0` は無効化。キャッシュを疑うとき設定だけで切り分けられるようにする。"""

    def test_never_returns_a_value(self):
        cache = TranslationCache(maxsize=0)
        cache.put(key("はい"), "Yes")
        assert cache.get(key("はい")) is None
        assert cache.enabled is False

    def test_records_no_statistics(self):
        cache = TranslationCache(maxsize=0)
        cache.get(key("はい"))
        stats = cache.stats()
        assert (stats.hits, stats.misses, stats.size) == (0, 0, 0)


class TestKeySeparation:
    """キーの取り違えは「英語の生徒に中国語が出る」形で現れる。ここで固定する。"""

    @pytest.mark.parametrize(
        "other",
        [
            pytest.param(key("こんにちは", lang="zh"), id="target_lang"),
            pytest.param(key("こんにちは", engine="nllb"), id="engine"),
            pytest.param(key("こんにちは", version="v2"), id="model_version"),
            pytest.param(key("こんばんは"), id="source_text"),
        ],
    )
    def test_differing_field_is_a_different_entry(self, other):
        cache = TranslationCache(maxsize=10)
        cache.put(key("こんにちは"), "Hello")
        assert cache.get(other) is None

    def test_source_lang_is_part_of_the_key(self):
        cache = TranslationCache(maxsize=10)
        cache.put(key("こんにちは"), "Hello")
        other = CacheKey(
            source_text="こんにちは",
            source_lang="en",
            target_lang="en",
            engine="hy-mt2",
            model_version="v1",
        )
        assert cache.get(other) is None


class TestStatistics:
    def test_hit_rate_counts_lookups_not_entries(self):
        cache = TranslationCache(maxsize=10)
        cache.put(key("はい"), "Yes")
        cache.get(key("はい"))  # hit
        cache.get(key("はい"))  # hit
        cache.get(key("いいえ"))  # miss
        stats = cache.stats()
        assert (stats.hits, stats.misses) == (2, 1)
        assert stats.hit_rate == pytest.approx(2 / 3)

    def test_hit_rate_is_zero_before_any_lookup(self):
        assert TranslationCache(maxsize=10).stats().hit_rate == 0.0

    def test_clear_drops_entries_and_counters(self):
        cache = TranslationCache(maxsize=10)
        cache.put(key("はい"), "Yes")
        cache.get(key("はい"))
        cache.clear()
        stats = cache.stats()
        assert (stats.hits, stats.misses, stats.size) == (0, 0, 0)
        assert cache.get(key("はい")) is None
