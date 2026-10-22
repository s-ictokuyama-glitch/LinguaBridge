"""切断注入の帳簿（`scripts/replay_client.py`・#35）のユニットテスト。

固定するのは1点だけだが、それが判定の意味を決めている:

    **試験が自分で切ったぶんを「想定外の切断」に数えない。**

数えると「注入すればするほど不合格に近づく」という無意味な判定になる。
#32 の60分レポートは切断0で合格しており、「`disconnects` が 0 でなければ異常」
という読み方がそこに乗っているので、その意味を壊さないこと。

復元そのものの判定（どの seq が届いたか）は `scripts/acceptance.py` の
`restore_gaps` 側にあり、`tests/unit/test_acceptance.py` で固定している。
"""

from __future__ import annotations

from scripts.replay_client import Results, _count_disconnect


def test_injected_disconnect_is_not_counted_as_unexpected() -> None:
    results = Results()
    results.injected_pending.add("s3")

    _count_disconnect(results, "s3")

    assert results.disconnects == 0, "注入した切断が想定外の切断に混ざった"
    assert "s3" not in results.injected_pending, "1回の切断で1回だけ相殺されるべき"


def test_unexpected_disconnect_is_counted() -> None:
    results = Results()

    _count_disconnect(results, "s3")

    assert results.disconnects == 1


def test_only_the_injected_student_is_excused() -> None:
    """注入したのが s3 なら、同時に落ちた s4 は想定外として数える。"""
    results = Results()
    results.injected_pending.add("s3")

    _count_disconnect(results, "s4")
    _count_disconnect(results, "s3")

    assert results.disconnects == 1
    assert results.injected_pending == set()


def test_a_second_disconnect_after_injection_is_unexpected() -> None:
    """注入で1回切られた生徒が**もう一度**落ちたら、2回目は想定外。

    相殺を使い切らずに残すと、注入した生徒はその後どれだけ落ちても
    見逃されることになる。
    """
    results = Results()
    results.injected_pending.add("s3")

    _count_disconnect(results, "s3")  # 注入ぶん
    _count_disconnect(results, "s3")  # 本当に落ちた

    assert results.disconnects == 1
