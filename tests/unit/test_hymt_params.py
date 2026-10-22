"""Hy-MT2 の出力上限とモデル版の単体テスト（イシュー#26 B-5 / B-7）。

`max_tokens` は「壊れた入力での暴走生成を、入力の長さに見合った時間で必ず止める」
ためのもの。llama.cpp を読み込まずに検査できるよう純関数に切ってある。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.mt.hymt_engine import (
    MIN_MAX_TOKENS,
    OUTPUT_TOKEN_MARGIN,
    OUTPUT_TOKEN_RATIO,
    HyMt2Engine,
    max_tokens_for,
)


class TestMaxTokensFor:
    def test_short_input_gets_the_floor(self):
        # 「はい」級の入力でも訳文が途中で切れない下限を保証する
        assert max_tokens_for(1, cap=512) == MIN_MAX_TOKENS

    def test_grows_with_input_length(self):
        assert max_tokens_for(100, cap=512) > max_tokens_for(10, cap=512)

    def test_scales_by_ratio_and_margin(self):
        assert max_tokens_for(100, cap=512) == int(100 * OUTPUT_TOKEN_RATIO) + OUTPUT_TOKEN_MARGIN

    def test_never_exceeds_the_cap(self):
        # 上限が効かないと、壊れた長大入力がそのまま生成時間になる
        assert max_tokens_for(10_000, cap=512) == 512

    def test_cap_below_the_floor_is_rejected(self):
        with pytest.raises(ValueError):
            max_tokens_for(10, cap=8)

    def test_zero_and_negative_input_are_safe(self):
        assert max_tokens_for(0, cap=512) == MIN_MAX_TOKENS
        assert max_tokens_for(-5, cap=512) == MIN_MAX_TOKENS


class TestModelVersion:
    """デコード設定を含めないと、設定変更後に古い訳がキャッシュから返る（#26 B-4）。"""

    def _engine(self, tmp_path: Path, temperature: float) -> HyMt2Engine:
        gguf = tmp_path / "Hy-MT2-1.8B-Q4_K_M.gguf"
        gguf.write_bytes(b"stub")  # 存在チェックを通すだけ。ロードはしない
        return HyMt2Engine(gguf, temperature=temperature)

    def test_greedy_and_sampling_are_different_versions(self, tmp_path):
        greedy = self._engine(tmp_path, 0.0).model_version
        sampling = self._engine(tmp_path, 0.7).model_version
        assert greedy != sampling

    def test_version_names_the_model_file(self, tmp_path):
        assert "Hy-MT2-1.8B-Q4_K_M.gguf" in self._engine(tmp_path, 0.0).model_version

    def test_same_settings_give_the_same_version(self, tmp_path):
        assert self._engine(tmp_path, 0.0).model_version == self._engine(tmp_path, 0.0).model_version
