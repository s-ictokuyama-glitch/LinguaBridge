"""CER/WER 算出（#24 ベースライン計測）のユニットテスト。

数えているのは「編集距離の内訳」と「正規化がどこまで畳むか」の2点。
正規化を緩めすぎると精度差が消え、厳しすぎると表記仕様の差が精度差に化けるので、
畳む/畳まないの境界をテストで固定する。
"""

from __future__ import annotations

import pytest

from scripts.text_metrics import (
    EditCounts,
    cer_counts,
    corpus_rate,
    edit_counts,
    normalize_ja,
    resolve_tokenizer,
    tokenize_charclass,
    wer_counts,
)


# ---- 正規化 ----


def test_normalize_drops_punctuation_and_space():
    # 句読点を出せないエンジン（#21 の ReazonSpeech K2）を表記仕様で減点しないため
    assert normalize_ja("今日は、晴れです。") == "今日は晴れです"
    assert normalize_ja("あ い\u3000う") == "あいう"


def test_normalize_keeps_punctuation_when_asked():
    assert normalize_ja("今日は、晴れです。", drop_marks=False) == "今日は、晴れです。"


def test_normalize_folds_halfwidth_kana_and_fullwidth_digits():
    assert normalize_ja("ｱｲｳ") == "アイウ"
    assert normalize_ja("４２ページ") == "42ページ"


def test_normalize_does_not_fold_homophone_errors():
    # 「光合成→構合性」は精度の誤りであって表記ゆれではない。畳んではいけない
    assert normalize_ja("光合成") != normalize_ja("構合性")


# ---- 編集距離 ----


def test_edit_counts_identical_is_zero():
    c = edit_counts("あいう", "あいう")
    assert c.distance == 0 and c.error_rate == 0.0


def test_edit_counts_splits_substitution_deletion_insertion():
    assert edit_counts("あいう", "あXう") == EditCounts(substitutions=1, ref_len=3)
    assert edit_counts("あいう", "あう") == EditCounts(deletions=1, ref_len=3)
    assert edit_counts("あいう", "あいうえ") == EditCounts(insertions=1, ref_len=3)


def test_error_rate_is_none_when_reference_is_empty():
    # noise-only クリップ（正解テキストが空）は誤り率が定義できない。
    # 幻覚の量は挿入文字数で見る
    c = edit_counts("", "あーうー")
    assert c.error_rate is None
    assert c.insertions == 4


# ---- CER ----


def test_cer_counts_homophone_error():
    c = cer_counts("今日は植物の光合成について勉強します。", "今日は植物の構合性について勉強します")
    assert (c.substitutions, c.deletions, c.insertions) == (2, 0, 0)
    assert c.ref_len == 18  # 句読点を除いた文字数
    assert c.error_rate == pytest.approx(2 / 18)


def test_cer_ignores_trailing_punctuation_difference():
    assert cer_counts("はい、それでは。", "はい それでは").distance == 0


def test_cer_counts_punctuation_when_marks_kept():
    assert cer_counts("はい、それでは。", "はい それでは", drop_marks=False).distance > 0


# ---- WER ----


def test_charclass_tokenizer_splits_on_script_boundaries():
    assert tokenize_charclass("光合成について") == ["光合成", "について"]
    assert tokenize_charclass("42ページ") == ["42", "ページ"]


def test_wer_counts_uses_tokens_not_characters():
    # 3文字の誤りでも1トークンの置換
    c = wer_counts("光合成について", "構合性について")
    assert c == EditCounts(substitutions=1, ref_len=2)


def test_resolve_tokenizer_falls_back_when_sudachi_missing():
    # 形態素解析器の採否は #27 の決定。未導入環境でも WER が出せること
    name, tokenize = resolve_tokenizer("sudachi")
    assert name in ("sudachi", "charclass")
    assert tokenize("光合成")


def test_resolve_tokenizer_rejects_unknown_name():
    with pytest.raises(ValueError):
        resolve_tokenizer("mecab")


# ---- コーパス集計 ----


def test_corpus_rate_weights_by_reference_length_not_by_clip():
    # 長いクリップ1件（誤り0/100）と短いクリップ1件（誤り1/2）。
    # クリップ平均なら 0.25 だが、総和なら 1/102
    counts = [EditCounts(ref_len=100), EditCounts(substitutions=1, ref_len=2)]
    assert corpus_rate(counts) == pytest.approx(1 / 102)


def test_corpus_rate_is_none_when_all_references_empty():
    assert corpus_rate([EditCounts(insertions=3, ref_len=0)]) is None
