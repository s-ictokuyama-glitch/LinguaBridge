"""#48: 先生向け運用手順書（README）の配布パッケージの手順。

手順書に書いた NLLB への切替の1行を、そのまま data\\config.yaml に書いたときに
配布用の既定の設定へ重なって NLLB に切り替わることを確かめる（手順書と実装のずれを防ぐ）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.build_package import write_distribution_config
from server.config import load_config

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def teacher_guide() -> str:
    start = README.index("# 先生向け運用手順書")
    return README[start:README.index("\n# 開発者向け", start)]


def section(heading: str) -> str:
    guide = teacher_guide()
    start = guide.index(f"\n## {heading}")
    end = guide.find("\n## ", start + 1)
    return guide[start:] if end < 0 else guide[start:end]


def nllb_switch_lines() -> list[str]:
    blocks = re.findall(r"```yaml\n(.*?)```", section("翻訳が遅いとき（NLLB への切替）"), re.S)
    assert len(blocks) == 1
    return [line.strip() for line in blocks[0].splitlines() if line.strip()]


def test_guide_covers_every_distribution_step():
    guide = teacher_guide()
    install = section("導入（配布パッケージ・このPCで1回だけ）")

    # 展開の前に「ブロックの解除」をする（手順の順番も確かめる）
    assert install.index("ダウンロード") < install.index("ブロックの解除") < install.index("すべて展開")
    assert "C:\\LinguaBridge\\" in install
    assert "ドキュメントやデスクトップには置かない" in install
    assert "「はい」" in install
    assert "SmartScreen" in install and "「詳細情報」→「実行」" in install
    assert "`app` フォルダを差し替える" in section("新しい版への更新")
    assert "data\\config.yaml" in section("翻訳が遅いとき（NLLB への切替）")
    assert "CC-BY-NC" in section("翻訳が遅いとき（NLLB への切替）")
    assert "非商用" in guide


@pytest.mark.parametrize("encoding, newline", [("utf-8", "\n"), ("utf-8-sig", "\r\n")])
def test_one_line_in_data_config_switches_to_nllb(tmp_path, encoding, newline):
    lines = nllb_switch_lines()
    assert len(lines) == 1
    app_config = tmp_path / "app" / "config.yaml"
    app_config.parent.mkdir()
    write_distribution_config(ROOT / "config.yaml", app_config)
    # メモ帳で保存したとき（BOM 付き・CRLF でも）と同じ形で data に置く
    override = tmp_path / "data" / "config.yaml"
    override.parent.mkdir()
    override.write_text(lines[0] + newline, encoding=encoding, newline="")

    base = load_config(app_config)
    switched = load_config(app_config, override=override, data_root=tmp_path / "data")

    assert base.mt.engine == "hy-mt2"
    assert switched.mt.engine == "nllb"
    # 同梱の NLLB（本体とトークナイザ）を app 内のモデルから読む
    assert switched.models.resolve(switched.mt.nllb.model_dir) == \
        base.models.resolve("nllb-200-distilled-600M-ct2")
    assert switched.models.resolve(switched.mt.nllb.tokenizer_dir) == base.models.resolve("nllb-tokenizer")
    # 翻訳エンジン以外は既定のまま
    assert switched.model_dump(exclude={"mt"}) == \
        load_config(app_config, data_root=tmp_path / "data").model_dump(exclude={"mt"})
    assert switched.mt.model_dump(exclude={"engine"}) == base.mt.model_dump(exclude={"engine"})
