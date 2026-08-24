"""ASR テキストの末尾がどういう「切れ目」かを分類する（#27 / Parapper R-3）。

Turn を確定してよいのは末尾が **文として終わっている**ときだけ。
文中の境界では絶対に分割しない、が不変条件。

    StrongEnd     句点で終わる                    → 即確定
    PredicateEnd  用言の終止形（〜ます / 〜です）   → 即確定
    NormalEnd     名詞・感動詞など                 → morph戦略なら継続
    ClauseWeak    読点・接続助詞（〜ので / 〜けど） → 継続
    Reject        助詞止め・条件形                 → 継続

**句読点より継続マーカーを優先する。** whisper は「単独で書き起こした断片」の末尾に
句点を付ける癖があり（#24 のベースライン実測: `次回はですね。`）、`。` は
「文が終わった」証拠ではなく「セグメントが終わった」証拠でしかない。
先に語幹の継続マーカーを見て、それが無いときだけ句点を信用する。

分類器は差し替え可能なシームにしてある。#28 で ReazonSpeech K2（句読点を出さない）を
採ったときの実測では、**このシームを使う必要は無かった** — `predicate_end` が
`strong_end` の役目をそのまま引き受け、区切りは whisper 版と1件も違わなかった。
形態素解析器版が要るとすれば、敬体で終わらない話し言葉（`〜だ`/`〜である` 調・
体言止め）が実授業で問題になったときで、そのときはここに差し込む。呼び出し側は変えない。
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Protocol


class BoundaryClass(Enum):
    """末尾の切れ目の強さ。`ends_turn` が「ここで Turn を確定してよいか」。"""

    STRONG_END = "strong_end"
    PREDICATE_END = "predicate_end"
    NORMAL_END = "normal_end"
    CLAUSE_WEAK = "clause_weak"
    REJECT = "reject"

    @property
    def ends_turn(self) -> bool:
        """morph 戦略で Turn を即確定してよいクラスか。

        NormalEnd を継続側に置くのは Parapper R-3 の morph 戦略に合わせたもの。
        「はい」「えーと」「それでは」のような感動詞・名詞止めは、
        後続がある可能性が高い（#24 実測の hes-03 がまさにこの形）。
        """
        return self in (BoundaryClass.STRONG_END, BoundaryClass.PREDICATE_END)


class BoundaryClassifier(Protocol):
    def classify(self, text: str) -> BoundaryClass: ...


# 末尾から剥がす記号。剥がした残り（語幹）に対して継続マーカーを判定する
_TRAILING = "。、，,．.！？!?」』）)　 \t\r\n"
# 句点として扱う文字（剥がす前の元テキストで判定する）
_SENTENCE_FINAL = "。！？!?．."

# 継続マーカー: ここで切ると文が途中で割れる。**句点判定より先に見る**
#
# `ですね` / `ますね` を継続に入れているのが要点。終助詞「ね」を StrongEnd に
# 入れると、授業の話し言葉に頻出する「この部分がですね……」を必ず割ってしまう
# （#24 実測 hes-02）。「そうですね。」のような正当な文末は force_silence_ms の
# 経過で確定するので、失うのは遅延だけで、分割は失わない。
_CLAUSE_WEAK_TAILS = (
    "ですね",
    "ますね",
    "ですけど",
    "ますけど",
    "ですが",
    "ますが",
    "けれども",
    "けれど",
    "けども",
    "けど",
    "だけど",
    "ので",
    "んで",
    "のに",
    "から",
    "ながら",
    "たり",
    "とか",
    "まして",
    "でして",
    # 動詞のテ形。文中で最も多い継続形（「光のエネルギーを使って」）。
    # 「立て」等の命令形と衝突しうるが、授業の話し言葉では継続の方が圧倒的に多い
    "て",
)

# 助詞・条件形止め。文法的に確実に続きがある
_REJECT_TAILS = (
    "ならば",
    "なら",
    "たら",
    "れば",
    "えば",
    "けば",
    "せば",
    "てば",
    "ねば",
    "べば",
    "めば",
)
# 1文字の助詞止め（語幹が1文字だけのときは助詞と見なさない）
_REJECT_PARTICLES = "はがをにへとももやのかでし"

# 感動詞・接続詞のフィラー。末尾の1文字がたまたま助詞と同じでも助詞止めではない
# （「えーと」の「と」、「それでは」の「は」）。分類名を実態に合わせるためだけの表で、
# NormalEnd も Reject も morph 戦略では同じく「継続」になる
_FILLERS = (
    "えーと",
    "えっと",
    "ええと",
    "えー",
    "あのー",
    "あの",
    "そのー",
    "その",
    "まあ",
    "はい",
    "それでは",
    "では",
    "さて",
    "ええ",
)

# 疑問の終止形。末尾の「か」は助詞止め判定より先に見ないと Reject に落ちる
_INTERROGATIVE_TAILS = (
    "ますか",
    "ですか",
    "ましたか",
    "でしたか",
    "でしょうか",
    "ませんか",
    "ましょうか",
)

# 終止形（用言の言い切り）。ここで切ってよい
_PREDICATE_TAILS = (
    "ませんでした",
    "ましょう",
    "でしょう",
    "ください",
    "くださる",
    "ました",
    "ません",
    "でした",
    "である",
    "であります",
    "ます",
    "です",
    "だった",
    "かった",
    "なかった",
    "ない",
    "ます",
)
# 動詞の終止形（ウ段で終わる漢字＋かな、または「する」「した」など）。
# 形態素解析器なしの近似なので、ひらがな1文字だけの一致は採らない
_VERB_FINAL = re.compile(r"(?:[ぁ-んァ-ヴ一-龠々]{2,})(?:する|した|しない|なる|なった|いる|いた|ある|あった)$")


def _stem(text: str) -> str:
    """末尾の句読点・閉じ括弧・空白を剥がした語幹。"""
    return text.rstrip(_TRAILING)


class SurfaceBoundaryClassifier:
    """形態素解析器を使わない表層（文字列）判定。

    句読点を手がかりに使うが、**それだけに依存してはいない**。ReazonSpeech K2 は
    句読点を出さないので StrongEnd が1件も取れないが（#28 実測: strong_end 0回）、
    日本語の敬体が `〜です`/`〜ます`/`〜ください` で終わるため PredicateEnd(15回) が
    その役目を引き受け、区切りの結果は whisper 版と1件も違わなかった。
    """

    def classify(self, text: str) -> BoundaryClass:
        raw = text.strip()
        if not raw:
            # 空文字は「切れ目が無い」= 継続扱い。空の Turn を確定させない
            return BoundaryClass.REJECT

        stem = _stem(raw)
        if not stem:
            # 記号だけ。分割の根拠にはしない
            return BoundaryClass.REJECT

        # 0) フィラー（感動詞・接続詞）。助詞止めに誤分類されないよう先に抜く
        if stem in _FILLERS:
            return BoundaryClass.NORMAL_END

        # 1) 疑問の終止形。「〜ますか」の「か」を助詞止めと読ませない
        if stem.endswith(_INTERROGATIVE_TAILS):
            return (
                BoundaryClass.STRONG_END
                if raw[-1] in _SENTENCE_FINAL
                else BoundaryClass.PREDICATE_END
            )

        # 2) 継続マーカーが最優先（句点より先）
        if stem.endswith(_CLAUSE_WEAK_TAILS):
            return BoundaryClass.CLAUSE_WEAK
        if stem.endswith(_REJECT_TAILS):
            return BoundaryClass.REJECT
        if len(stem) >= 2 and stem[-1] in _REJECT_PARTICLES:
            return BoundaryClass.REJECT

        # 3) 剥がす前に句点で終わっていたか（継続マーカーが無いときだけ信用する）
        if raw[-1] in _SENTENCE_FINAL:
            return BoundaryClass.STRONG_END

        # 4) 用言の終止形
        if stem.endswith(_PREDICATE_TAILS) or _VERB_FINAL.search(stem):
            return BoundaryClass.PREDICATE_END

        # 5) 名詞・感動詞など。morph 戦略では継続側に置く
        return BoundaryClass.NORMAL_END


def build_boundary_classifier(kind: str = "surface") -> BoundaryClassifier:
    """設定から分類器を作る。形態素解析器版が要るようになればここに増える。"""
    if kind == "surface":
        return SurfaceBoundaryClassifier()
    raise ValueError(f"未知の境界分類器: {kind}")
