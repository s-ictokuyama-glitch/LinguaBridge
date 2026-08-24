"""文法境界の分類（#27 / server/turn/boundary.py）のユニットテスト。

不変条件は「文中の境界では絶対に分割しない」。したがって、
**継続クラスの取りこぼし（＝切ってはいけない所を切る）を落とすテストを厚くする。**
逆向きの誤り（切れる所で切らない）は遅延が force_silence_ms ぶん伸びるだけで、
字幕は壊れない。
"""

from __future__ import annotations

import pytest

from server.turn.boundary import BoundaryClass, SurfaceBoundaryClassifier


@pytest.fixture
def classify():
    return SurfaceBoundaryClassifier().classify


class TestStrongEnd:
    @pytest.mark.parametrize(
        "text",
        [
            "光合成には日光と水と二酸化炭素が必要です。",
            "教科書の四十二ページを開いてください。",
            "質問はありますか？",
            "静かにしなさい！",
            "Is that clear?",
        ],
    )
    def test_sentence_final_punctuation_ends_turn(self, classify, text):
        assert classify(text) is BoundaryClass.STRONG_END
        assert classify(text).ends_turn


class TestPredicateEnd:
    @pytest.mark.parametrize(
        "text",
        [
            # #24 のベースライン実測: whisper は句点を付けないことがある
            "昨日の実験の結果をグループごとに発表してもらいます",
            "この実験で一番大事なのは温度をきちんと管理することです",
            "先週の授業でデンプンについて学びました",
            "教科書を開いてください",
            "次のページを見てみましょう",
            "それでは実験を始めます",
        ],
    )
    def test_plain_predicate_ends_turn(self, classify, text):
        assert classify(text) is BoundaryClass.PREDICATE_END
        assert classify(text).ends_turn


class TestContinuation:
    @pytest.mark.parametrize(
        "text",
        [
            # #24 実測の rate-slow-b。現行の3件の不自然な分割のうちの1件
            "この化学反応式の左側と右側で",
            "教科書の四十二ページの",
            "昨日の実験の結果を",
            "植物が光のエネルギーを使って",
            "温度が上がれば",
            "うまくいかなかったら",
        ],
    )
    def test_particle_and_conditional_do_not_end_turn(self, classify, text):
        assert classify(text) in (BoundaryClass.REJECT, BoundaryClass.CLAUSE_WEAK)
        assert not classify(text).ends_turn

    @pytest.mark.parametrize(
        "text",
        [
            "今日の内容はここまでなんですけれども",
            "この部分がですね",
            "実験の準備ができたので",
            "時間がないから",
            "少し難しいと思うんですけど",
            "図を見ながら",
        ],
    )
    def test_clause_connectors_do_not_end_turn(self, classify, text):
        assert classify(text) is BoundaryClass.CLAUSE_WEAK
        assert not classify(text).ends_turn


class TestPunctuationIsNotTrusted:
    """whisper は断片の末尾に句点を付ける癖がある（#24 実測: `次回はですね。`）。

    継続マーカーが立っているときは、句点があっても切らない。これを外すと
    min_silence_ms を 320ms に下げた瞬間に文中での分割が増える。
    """

    @pytest.mark.parametrize(
        "text",
        [
            "この部分がですね。",
            "今日の内容はここまでなんですけれども。",
            "この化学反応式の左側と右側で、",
            "実験の準備ができたので。",
        ],
    )
    def test_continuation_wins_over_sentence_final_mark(self, classify, text):
        assert not classify(text).ends_turn

    def test_punctuation_still_trusted_without_continuation_marker(self, classify):
        assert classify("葉緑体という器官があります。") is BoundaryClass.STRONG_END


class TestNormalEnd:
    """名詞・感動詞止め。morph 戦略では継続side（Parapper R-3）。"""

    @pytest.mark.parametrize("text", ["はい", "えーと", "それでは", "葉緑体", "光合成"])
    def test_noun_and_filler_do_not_end_turn(self, classify, text):
        assert classify(text) is BoundaryClass.NORMAL_END
        assert not classify(text).ends_turn


class TestDegenerateInput:
    @pytest.mark.parametrize("text", ["", "   ", "。", "、", "…"])
    def test_empty_or_symbol_only_never_ends_turn(self, classify, text):
        """空・記号だけで Turn を確定させない（空カードを作らない）。"""
        assert not classify(text).ends_turn
