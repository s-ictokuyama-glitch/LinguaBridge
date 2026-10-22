"""案内する次のコマンド（証明書CLI・接続診断）の起動部分（#51）。

配布パッケージには .venv が無く、同梱の app\\python\\python.exe で動く。実行中の
インタープリターが同梱版なら絶対パスで案内し、どのフォルダからでもそのまま実行できる
ようにする（同梱版の ._pth が app を sys.path に入れるので -m server.diagnostics も動く）。
それ以外（開発機）は従来どおり、リポジトリのルートで実行する .venv のコマンドを案内する。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class LaunchCommands:
    make_cert: str  # 後ろに --config 等の引数を続ける
    diagnostics: str


DEVELOPMENT_COMMANDS = LaunchCommands(
    make_cert=r".venv\Scripts\python scripts\make_cert.py",
    diagnostics=r".venv\Scripts\python -m server.diagnostics",
)


def launch_commands(executable: str | None = None, app_root: Path = ROOT) -> LaunchCommands:
    """実行中のインタープリター（既定: sys.executable）が同梱版かで起動形態を判定する。"""
    python = sys.executable if executable is None else executable
    bundled = app_root / "python" / "python.exe"  # scripts/launcher.ps1 の配布モード判定と同じ置き場所
    # ジャンクション等を経由した展開先でも同じ実体なら同梱版とみなす
    if os.path.normcase(Path(python).resolve()) != os.path.normcase(bundled.resolve()):
        return DEVELOPMENT_COMMANDS
    return LaunchCommands(
        make_cert=f"{_quoted(python)} {_quoted(str(app_root / 'scripts' / 'make_cert.py'))}",
        diagnostics=f"{_quoted(python)} -m server.diagnostics",
    )


def _quoted(path: str) -> str:
    # 引用符で始まる行は PowerShell では先頭に & が要る。既定の C:\LinguaBridge のように
    # 空白が無ければ囲まず、コマンドプロンプトと PowerShell のどちらにもそのまま貼れるようにする。
    return f'"{path}"' if any(char.isspace() for char in path) else path
