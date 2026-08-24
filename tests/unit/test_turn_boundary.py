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


class TestPlainForms:
    """敬体で終わらない言い切り（#34）。

    #34 以前は `〜です`/`〜ます`/`〜ください` しか言い切りと認めていなかったため、
    常体（〜だ調）と漢字表記の「下さい」を取りこぼし、`force_silence_ms`(1000ms) の
    無音待ちに落ちていた。既定構成で実際に1件出ていた（`前に出てきて下さい`）。
    """

    @pytest.mark.parametrize(
        "text",
        [
            # ReazonSpeech が実際に漢字で書いた形（#34 の発端）
            "準備ができた班から前に出てきて下さい",
            "教科書を開いて下さい",
        ],
    )
    def test_kanji_polite_ends_turn(self, classify, text):
        assert classify(text) is BoundaryClass.PREDICATE_END
        assert classify(text).ends_turn

    @pytest.mark.parametrize(
        "text",
        [
            "これが光合成だ",
            "ここが大事だな",
            "よし、始めるぞ",
            "これは覚えておくべきだよ",
            "答えはこうなるだろう",
        ],
    )
    def test_plain_copula_ends_turn(self, classify, text):
        assert classify(text) is BoundaryClass.PREDICATE_END
        assert classify(text).ends_turn

    @pytest.mark.parametrize(
        "text", ["分かったか", "図が見えるか", "これでいいのか"]
    )
    def test_plain_interrogative_ends_turn(self, classify, text):
        """常体の疑問形。末尾の「か」は助詞止めの表にあるので、順序を守らないと
        Reject に落ちる（#34 以前はまさにそうなっていた）。"""
        assert classify(text) is BoundaryClass.PREDICATE_END
        assert classify(text).ends_turn

    @pytest.mark.parametrize(
        "text",
        [
            # 名詞＋「か」。「何人か」「いくつか」のような不定の名詞句と表層で区別できない
            "ここまで大丈夫か",
            "全員そろったのは何人か",
            # 音便の過去形＋「か」。「なんだか」が同じ `〜んだか` の形なので、
            # ここを拾うと継続の副詞を確定側へ落とす
            "ちゃんと読んだか",
        ],
    )
    def test_interrogatives_left_out_on_purpose(self, classify, text):
        """**拾わないと決めた疑問形**（意図的な線引き。取りこぼしは1秒の遅れで済む）。

        疑問形として拾うのは用言の活用（`〜たか`/`〜るか`/`〜いか`/`〜のか`/`〜んか`）だけ。
        ここを広げるなら形態素解析器が要る — 表層では**曖昧さが残る側に倒す**、が #27 の方針。
        """
        assert not classify(text).ends_turn


class TestPlainFormsDoNotOverreach:
    """「〜だ」を言い切りに入れた副作用の回帰（#34）。

    ここが赤くなる = 文中で割るようになった、なので #27 の不変条件違反。
    """

    @pytest.mark.parametrize(
        "text",
        [
            # 末尾が「だ」の副詞・接続詞。言い切りではない
            "ただ",
            "まだ",
            # 「か」で終わるが疑問形ではない
            "とか",
            "何人か",
            "いくつか",
            # 名詞・助詞止めは #34 でも継続のまま
            "教科書の",
            "光合成というのは",
            "という",
        ],
    )
    def test_lookalikes_do_not_end_turn(self, classify, text):
        assert not classify(text).ends_turn


class TestTaigenStaysContinuation:
    """体言止めは確定側へ動かさない（#34 の判断）。

    「じゃあ次、教科書」（完結）と「教科書」（言いよどみ）は**表層では区別できない**。
    確定側へ寄せると後者で文中で割れる（プロトタイプ実測: `taigen-01` で2件）。
    体言止めを扱うには形態素解析器が要る＝別の問い。
    """

    @pytest.mark.parametrize("text", ["じゃあ次、教科書", "教科書", "今日の授業はここまで"])
    def test_noun_final_does_not_end_turn(self, classify, text):
        assert not classify(text).ends_turn


class TestPoliteOnlyControlGroup:
    """`plain_forms=False` が #34 以前の分類を返すこと（A/B の対照群が本当に対照群か）。

    これが赤い = ベンチの before が before でなくなっているので、A/B の数字が読めない。
    """

    @pytest.fixture
    def before(self):
        return SurfaceBoundaryClassifier(plain_forms=False).classify

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("準備ができた班から前に出てきて下さい", BoundaryClass.NORMAL_END),
            ("これが光合成だ", BoundaryClass.NORMAL_END),
            ("よし、始めるぞ", BoundaryClass.NORMAL_END),
            ("分かったか", BoundaryClass.REJECT),
        ],
    )
    def test_reproduces_pre_34_classes(self, before, text, expected):
        assert before(text) is expected
        assert not before(text).ends_turn

    @pytest.mark.parametrize(
        "text",
        ["前に出てきてください", "実験を始めます", "この部分がですね", "教科書の"],
    )
    def test_untouched_cases_are_identical(self, before, classify, text):
        """#34 が触っていない語尾は両者で完全に一致する（差分が #34 のぶんだけであること）。"""
        assert before(text) is classify(text)
