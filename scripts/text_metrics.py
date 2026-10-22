"""日本語ASRの精度指標（イシュー#24 ベースライン計測）。

CER を主指標にする。日本語は語境界が表層に無く、WER は形態素解析器の切り方に
数値が丸ごと依存するため、単独では機種間比較に使えない。

**句読点は既定で採点対象から外す**。#21 で判明した通り ReazonSpeech K2 v2 は
語彙に `。！？` を持たず句読点を出力できないので、句読点込みで採点すると
ASRエンジンの判定（#28）が表記仕様の差で決まってしまう。素の CER も併記する。

WER のトークナイザは差し替え可能にしてある（既定は文字種の切れ目で切る近似）。
形態素解析器を入れるかどうかは #27 の決定事項なので、ここでは依存を増やさない。
出力には常に使ったトークナイザ名を残し、別トークナイザの数値と比べないようにする。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

# 採点から外す記号（句読点・括弧・記号類）。Unicode一般カテゴリ P*/S* に加え、
# 全角スペース等の空白も落とす。数字・かなカナ漢字・ラテン文字は残る。
_SPACE_RE = re.compile(r"\s+")


def strip_marks(text: str) -> str:
    """句読点・記号・空白を落とす（採点用の正規化の一部）。"""
    return "".join(
        ch for ch in text if not unicodedata.category(ch).startswith(("P", "S", "Z"))
    )


def normalize_ja(text: str, *, drop_marks: bool = True) -> str:
    """採点用の正規化。NFKC → 空白除去 →（既定で）句読点・記号除去。

    NFKC で半角カナ・全角英数の表記ゆれを畳む。エンジンごとの表記仕様の差を
    精度の差として数えないための最小限の正規化に留める（表記ゆれ辞書は入れない）。
    """
    text = unicodedata.normalize("NFKC", text)
    text = _SPACE_RE.sub("", text)
    return strip_marks(text) if drop_marks else text


# ---- トークナイザ（WER用） ----

_CHARCLASS_RE = re.compile(
    r"[\u4E00-\u9FFF\u3005\u3007\u303B]+"  # 漢字
    r"|[\u3040-\u309F]+"  # ひらがな
    r"|[\u30A0-\u30FF\u31F0-\u31FF\u30FC]+"  # カタカナ
    r"|[A-Za-z]+"
    r"|[0-9]+"
    r"|[^\s]"  # それ以外は1文字1トークン
)


def tokenize_charclass(text: str) -> list[str]:
    """文字種の切れ目で切る近似トークナイザ。

    形態素解析器を持たない環境でも WER を出すための代用。「光合成」のような
    漢字連続は1トークンになり、活用語尾のひらがなは別トークンになる。
    **形態素単位の WER とは値が一致しない**ので、比較は同一トークナイザ内に限る。
    """
    return _CHARCLASS_RE.findall(text)


def tokenize_sudachi(text: str) -> list[str]:
    """SudachiPy（mode C）による形態素トークナイザ。未導入なら ImportError。"""
    from sudachipy import dictionary, tokenizer  # type: ignore[import-not-found]

    tok = dictionary.Dictionary().create()
    return [m.surface() for m in tok.tokenize(text, tokenizer.Tokenizer.SplitMode.C)]


TOKENIZERS: dict[str, Callable[[str], list[str]]] = {
    "charclass": tokenize_charclass,
    "sudachi": tokenize_sudachi,
}


def resolve_tokenizer(name: str) -> tuple[str, Callable[[str], list[str]]]:
    """トークナイザを解決する。sudachi が未導入なら charclass へ落ちる（名前も返す）。"""
    if name not in TOKENIZERS:
        raise ValueError(f"未知のトークナイザ: {name}（{sorted(TOKENIZERS)} のいずれか）")
    if name == "sudachi":
        try:
            tokenize_sudachi("テスト")
        except Exception:
            return "charclass", tokenize_charclass
    return name, TOKENIZERS[name]


# ---- 編集距離 ----


@dataclass(frozen=True)
class EditCounts:
    """編集操作の内訳。`ref_len` が 0 のとき誤り率は定義できない（None を返す）。"""

    substitutions: int = 0
    deletions: int = 0
    insertions: int = 0
    ref_len: int = 0

    @property
    def distance(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def error_rate(self) -> float | None:
        return self.distance / self.ref_len if self.ref_len else None

    def __add__(self, other: EditCounts) -> EditCounts:
        return EditCounts(
            self.substitutions + other.substitutions,
            self.deletions + other.deletions,
            self.insertions + other.insertions,
            self.ref_len + other.ref_len,
        )


def edit_counts(ref: Sequence[str], hyp: Sequence[str]) -> EditCounts:
    """Levenshtein 距離を S/D/I の内訳つきで返す（DPは2行のみ保持）。

    バックトレースを持たずに済ませるため、各セルで距離と内訳を同時に運ぶ。
    参照長 174.5秒ぶんのクリップ単位（最大数百トークン）を想定した実装で、
    長大テキストの一括採点は想定しない。
    """
    prev: list[EditCounts] = [
        EditCounts(insertions=j, ref_len=0) for j in range(len(hyp) + 1)
    ]
    for i in range(1, len(ref) + 1):
        cur: list[EditCounts] = [EditCounts(deletions=i, ref_len=0)]
        for j in range(1, len(hyp) + 1):
            if ref[i - 1] == hyp[j - 1]:
                cur.append(prev[j - 1])
                continue
            sub = prev[j - 1]
            dele = prev[j]
            ins = cur[j - 1]
            best = min(
                (sub.distance + 1, 0, sub),
                (dele.distance + 1, 1, dele),
                (ins.distance + 1, 2, ins),
                key=lambda t: (t[0], t[1]),  # 同点は S > D > I の順で決定的に選ぶ
            )
            _, kind, base = best
            if kind == 0:
                cur.append(EditCounts(base.substitutions + 1, base.deletions, base.insertions))
            elif kind == 1:
                cur.append(EditCounts(base.substitutions, base.deletions + 1, base.insertions))
            else:
                cur.append(EditCounts(base.substitutions, base.deletions, base.insertions + 1))
        prev = cur
    return EditCounts(prev[-1].substitutions, prev[-1].deletions, prev[-1].insertions, len(ref))


# ---- 指標 ----


def cer_counts(reference: str, hypothesis: str, *, drop_marks: bool = True) -> EditCounts:
    """文字誤り率の内訳。正規化後の文字列で数える。"""
    ref = normalize_ja(reference, drop_marks=drop_marks)
    hyp = normalize_ja(hypothesis, drop_marks=drop_marks)
    return edit_counts(ref, hyp)


def wer_counts(
    reference: str,
    hypothesis: str,
    tokenize: Callable[[str], list[str]] = tokenize_charclass,
    *,
    drop_marks: bool = True,
) -> EditCounts:
    """単語誤り率の内訳。トークナイザ依存の値である点に注意。"""
    ref = tokenize(normalize_ja(reference, drop_marks=drop_marks))
    hyp = tokenize(normalize_ja(hypothesis, drop_marks=drop_marks))
    return edit_counts(ref, hyp)


def corpus_rate(counts: Iterable[EditCounts]) -> float | None:
    """コーパス全体の誤り率＝編集距離の総和 ÷ 参照長の総和。

    クリップごとの誤り率を平均すると短いクリップが過大評価されるため、総和で取る。
    """
    total = EditCounts()
    for c in counts:
        total = total + c
    return total.error_rate
