"""スケーリングベンチ（`scripts/scale_bench.py`・#32）のユニットテスト。

このファイルが存在する理由は具体的で、**同じ失敗を二度と40分かけないため**。
`Results` のプロパティを1つ整理したとき、行列16点（1点2分）が全部
「計測は終わったが結果を組み立てる行で AttributeError」で落ちた。
サーバーを立てないと1点も走らない構造だったので、壊れていることに
40分かけて気づいた。結果の組み立てを純粋関数（`summarize`）に切り出し、
ここで固定する。

固定するのは3点:

    1. `summarize` は `Results` の実際の形で動く（属性を消したらここが赤くなる）
    2. `failed_point` は `summarize` と**同じキー**を返す
       （レポートの表は両方を同じ列に流し込むので、キーがずれると
         成功した点ではなく失敗した点の描画で落ちる）
    3. 実効言語数は `min(生徒数, 言語数)` — 生徒1名×5言語は実質1言語であり、
       この点を言語軸の主張に使ってはいけない
"""

from __future__ import annotations

from scripts.replay_client import Results
from scripts.scale_bench import failed_point, summarize


def make_results(*, captions: dict | None = None, stats: dict | None = None) -> Results:
    results = Results()
    results.captions = captions if captions is not None else {(1, "en"): 500, (2, "en"): 1500}
    results.stats_samples = [
        {
            "asr_calls": 7,
            "mt_calls": 5,
            "mt_cache_hits": 2,
            "tasks": 31,
            "audio_frames_rejected": 0,
            "asr_dropped_segments": 0,
            "asr_dropped_seconds": 0.0,
            **(stats or {}),
        }
    ]
    return results


def test_summarize_reads_the_real_results_shape() -> None:
    """`Results` の属性が消えたらここが落ちる（サーバーを立てずに）。"""
    row = summarize(
        make_results(), langs=["en", "zh"], students=10, mt_engine="hy-mt2", asr_engine="sherpa"
    )
    assert row["asr_calls"] == 7
    assert row["mt_calls"] == 5
    assert row["dropped_segments"] == 0
    assert row["frames_rejected"] == 0
    assert row["captions"] == 2
    assert row["median_delay_s"] == 1.5  # sorted([500,1500])[1] / 1000
    assert row["max_delay_s"] == 1.5
    assert row["asr_engine"] == "sherpa"


def test_summarize_survives_a_point_that_produced_nothing() -> None:
    """caption も stats も 0 件でも行は作れる（None として出る）。"""
    row = summarize(
        Results(), langs=["en"], students=1, mt_engine="fake", asr_engine=None
    )
    assert row["captions"] == 0
    assert row["median_delay_s"] is None
    assert row["asr_calls"] == 0
    assert row["dropped_segments"] == 0


def test_failed_point_has_the_same_keys_as_a_measured_point() -> None:
    """失敗した点も同じ列に流し込まれるので、キーがずれてはいけない。"""
    ok = summarize(
        make_results(), langs=["en", "zh"], students=10, mt_engine="hy-mt2", asr_engine="sherpa"
    )
    bad = failed_point(["en", "zh"], 10, "hy-mt2", "TimeoutError: /ready が来ない")
    assert set(ok) <= set(bad), f"失敗点に足りないキー: {set(ok) - set(bad)}"
    assert bad["failed"]


def test_active_langs_is_capped_by_the_student_count() -> None:
    """生徒1名 × 5言語は実効1言語。言語軸の主張にこの点は使えない。"""
    row = summarize(
        make_results(),
        langs=["en", "zh", "pt", "vi", "ko"],
        students=1,
        mt_engine="fake",
        asr_engine="sherpa",
    )
    assert row["lang_count"] == 5
    assert row["active_langs"] == 1

    many = summarize(
        make_results(),
        langs=["en", "zh", "pt", "vi", "ko"],
        students=40,
        mt_engine="fake",
        asr_engine="sherpa",
    )
    assert many["active_langs"] == 5
