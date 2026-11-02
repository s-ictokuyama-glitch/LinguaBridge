"""モデルファイルの起動時検証（plan.md E-13）。

存在チェックに加えて最小サイズを検証し、ダウンロード中断などによる
不完全なモデルでの起動（warmup中の不可解なクラッシュ）を防ぐ。
エンジンのファクトリ（server/main.py の build_*_engine）から呼ばれる。
"""

from __future__ import annotations

from pathlib import Path

_GUIDANCE = "scripts/download_models.py を実行してモデルを取得してください"
TRANSLATION_GUIDANCE = "配布パッケージを展開し直してください"


class TranslationModelError(ValueError):
    """利用者へそのまま案内できる翻訳モデルの不備。"""


def _total_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def require_model_files(
    path: Path, min_bytes: int, what: str, *, translation_models_dir: Path | None = None,
) -> None:
    display_path = translation_models_dir if translation_models_dir is not None else path
    guidance = TRANSLATION_GUIDANCE if translation_models_dir is not None else _GUIDANCE
    if not path.exists():
        raise FileNotFoundError(f"{what}が見つからない: {display_path}\n{guidance}")
    size = _total_bytes(path)
    if size < min_bytes:
        raise FileNotFoundError(
            f"{what}が不完全な可能性: {display_path}（{size:,} bytes < 期待下限 {min_bytes:,}）\n"
            f"ダウンロードが中断された可能性があります。{guidance}"
        )
