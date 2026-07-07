"""モデルファイルの起動時検証（plan.md E-13）。

存在チェックに加えて最小サイズを検証し、ダウンロード中断などによる
不完全なモデルでの起動（warmup中の不可解なクラッシュ）を防ぐ。
エンジンのファクトリ（server/main.py の build_*_engine）から呼ばれる。
"""

from __future__ import annotations

from pathlib import Path

_GUIDANCE = "scripts/download_models.py を実行してモデルを取得してください"


def _total_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def require_model_files(path: Path, min_bytes: int, what: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{what}が見つからない: {path}\n{_GUIDANCE}")
    size = _total_bytes(path)
    if size < min_bytes:
        raise FileNotFoundError(
            f"{what}が不完全な可能性: {path}（{size:,} bytes < 期待下限 {min_bytes:,}）\n"
            f"ダウンロードが中断された可能性があります。{_GUIDANCE}"
        )
