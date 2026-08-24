"""Segment → Turn の連結（#27 / server/turn/assembler.py）のユニットテスト。

TurnAssembler はタイマーを持たないので、入力列だけで挙動が決まる。
`simple` が **#27 以前と完全に同一**であること（1 Segment = 1 Turn）が最重要。
"""

from __future__ import annotations

import pytest

from server.turn.assembler import TurnAssembler, TurnPart
from server.turn.boundary import SurfaceBoundaryClassifier


def part(text: str, *, t_start: float = 0.0, t_end: float = 1.0, asr_ms: int = 100) -> TurnPart:
    return TurnPart(
        text=text,
        t_start=t_start,
        t_end=t_end,
        asr_ms=asr_ms,
        closed_at=t_end,
        audio_s=t_end - t_start,
    )


def make(strategy: str = "morph", **kwargs) -> TurnAssembler:
    return TurnAssembler(SurfaceBoundaryClassifier(), strategy=strategy, **kwargs)


class TestSimpleStrategy:
    """既定。#27 以前と同じ「Segment 1つ = 字幕カード1枚」。"""

    def test_every_segment_becomes_its_own_turn(self):
        asm = make("simple")
        for text in ("この化学反応式の左側と右側で", "原子の数が同じです。", "はい"):
            turn = asm.add_segment(part(text))
            assert turn is not None
            assert turn.text == text
            assert turn.parts == 1
            assert not turn.merged
        assert not asm.has_pending

    def test_boundary_is_a_no_op(self):
        asm = make("simple")
        asm.add_segment(part("はい"))
        assert asm.on_boundary() is None  # 保留が無いので何も出ない


class TestMorphConfirmsImmediately:
    """StrongEnd / PredicateEnd は次を待たずに確定する（遅延を伸ばさない）。"""

    @pytest.mark.parametrize(
        "text",
        ["原子の数が同じになっていることを確認しましょう。", "昨日の実験の結果を発表してもらいます"],
    )
    def test_sentence_end_confirms_without_waiting(self, text):
        asm = make()
        turn = asm.add_segment(part(text))
        assert turn is not None
        assert turn.reason in ("strong_end", "predicate_end")
        assert not asm.has_pending


class TestMorphMerges:
    def test_particle_tail_waits_and_merges_with_next_segment(self):
        """#24 実測の rate-slow-b: `…右側で` / `原子の数が…` が1枚に戻る。"""
        asm = make()
        assert asm.add_segment(part("この化学反応式の左側と右側で", t_end=3.87)) is None
        assert asm.has_pending
        turn = asm.add_segment(
            part("原子の数が同じになっていることを確認しましょう。", t_start=4.38, t_end=8.83)
        )
        assert turn is not None
        assert turn.text == "この化学反応式の左側と右側で原子の数が同じになっていることを確認しましょう。"
        assert turn.parts == 2 and turn.merged
        assert turn.t_start == 0.0 and turn.t_end == 8.83

    def test_asr_ms_and_audio_are_summed_over_the_merged_segments(self):
        asm = make()
        asm.add_segment(part("この部分がですね", t_start=0.0, t_end=1.0, asr_ms=1200))
        turn = asm.add_segment(part("大事です。", t_start=1.4, t_end=2.4, asr_ms=1100))
        assert turn is not None
        assert turn.asr_ms == 2300
        assert turn.audio_s == pytest.approx(2.0)

    def test_delay_origin_is_the_last_segment(self):
        """delay_ms の起点は連結の**最後**。先頭にすると連結ぶんが遅延に化ける。"""
        asm = make()
        asm.add_segment(part("はい", t_start=0.0, t_end=0.4))
        turn = asm.add_segment(part("始めます。", t_start=1.0, t_end=2.0))
        assert turn is not None and turn.closed_at == 2.0


class TestMorphBoundary:
    def test_boundary_confirms_a_held_turn(self):
        """文法が「継続」でも、無音が force_silence_ms に達したら切る。"""
        asm = make()
        assert asm.add_segment(part("はい")) is None
        turn = asm.on_boundary()
        assert turn is not None
        assert turn.text == "はい" and turn.reason == "boundary"
        assert not asm.has_pending

    def test_boundary_without_pending_returns_nothing(self):
        assert make().on_boundary() is None

    def test_flush_confirms_a_held_turn(self):
        asm = make()
        asm.add_segment(part("この部分がですね"))
        turn = asm.flush()
        assert turn is not None and turn.reason == "flush"
        assert make().flush() is None


class TestMergeIsBounded:
    """継続クラスが続いても連結は暴走しない（45分の授業でカードが消えない）。"""

    def test_segment_count_is_capped(self):
        asm = make(max_segments=3)
        assert asm.add_segment(part("教科書の", t_start=0.0, t_end=0.5)) is None
        assert asm.add_segment(part("四十二ページの", t_start=0.6, t_end=1.1)) is None
        turn = asm.add_segment(part("下のほうの", t_start=1.2, t_end=1.7))
        assert turn is not None
        assert turn.reason == "max_turn" and turn.parts == 3

    def test_audio_span_is_capped(self):
        asm = make(max_turn_s=2.0)
        assert asm.add_segment(part("教科書の", t_start=0.0, t_end=0.5)) is None
        turn = asm.add_segment(part("図を見て", t_start=2.0, t_end=2.5))
        assert turn is not None and turn.reason == "max_turn"


class TestReset:
    def test_reset_drops_the_pending_turn(self):
        asm = make()
        asm.add_segment(part("この部分がですね"))
        asm.reset()
        assert not asm.has_pending and asm.flush() is None


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError, match="turn 戦略"):
        make("namo")


class TestTurnIdForPartials:
    """turn_id は `seq` と違い**確定前から**存在する（#29）。partial の宛先になる。"""

    def test_turn_ids_are_monotonic_across_turns(self):
        asm = make("morph")
        first = asm.add_segment(part("原子の数が同じです。"))
        second = asm.add_segment(part("次に進みます。"))
        assert first is not None and second is not None
        assert first.turn_id == 1
        assert second.turn_id == 2

    def test_all_segments_of_a_merged_turn_share_one_id(self):
        asm = make("morph")
        assert asm.add_segment(part("この化学反応式の左側と右側で")) is None
        reserved = asm.current_turn_id
        turn = asm.add_segment(part("原子の数が同じです。"))
        assert turn is not None and turn.turn_id == reserved

    def test_id_is_reserved_before_any_segment_arrives(self):
        """interim が先に来る場合。ASR を呼ぶ前に identity を固定する必要がある。"""
        asm = make("morph")
        reserved = asm.reserve_turn_id()
        assert asm.current_turn_id == reserved
        turn = asm.add_segment(part("原子の数が同じです。"))
        assert turn is not None and turn.turn_id == reserved

    def test_current_turn_id_clears_when_the_turn_closes(self):
        """stale 検出の土台。確定後に返ってきた partial は捨てられなければならない。"""
        asm = make("morph")
        asm.reserve_turn_id()
        assert asm.add_segment(part("原子の数が同じです。")) is not None
        assert asm.current_turn_id is None

    def test_preview_returns_the_whole_pending_turn_not_just_the_interim(self):
        """先生には Turn の全文が見える。interim だけ出すと確定時に文が前へ伸びる。"""
        asm = make("morph")
        assert asm.add_segment(part("この化学反応式の左側と右側で")) is None
        turn_id, revision, text = asm.preview("原子の数が")
        assert text == "この化学反応式の左側と右側で原子の数が"
        assert (turn_id, revision) == (1, 1)

    def test_revision_increases_within_a_turn_and_resets_for_the_next(self):
        asm = make("morph")
        assert asm.preview("この")[1] == 1
        assert asm.preview("この化学")[1] == 2
        assert asm.add_segment(part("原子の数が同じです。")) is not None
        assert asm.preview("次に")[:2] == (2, 1)  # 新しい turn / revision は 1 から

    def test_boundary_with_nothing_pending_releases_a_reserved_id(self):
        """partial だけ出て Segment が来なかった場合（幻覚破棄など）。

        解放しないと turn_id が居座り、次の発話の partial が古い Turn の続きとして
        先生UIに追記されてしまう。
        """
        asm = make("morph")
        asm.reserve_turn_id()
        assert asm.on_boundary() is None
        assert asm.current_turn_id is None

    def test_simple_strategy_also_carries_turn_ids(self):
        asm = make("simple")
        turn = asm.add_segment(part("はい"))
        assert turn is not None and turn.turn_id == 1
