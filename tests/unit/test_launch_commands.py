"""#51: 案内するコマンドを、実行中のインタープリターと起動形態から作る。"""

from __future__ import annotations

from pathlib import Path

from server.launch_commands import launch_commands


def test_bundled_python_gets_absolute_distribution_commands():
    app = Path(r"C:\LinguaBridge\app")

    commands = launch_commands(r"C:\LinguaBridge\app\python\python.exe", app)

    # 空白が無い既定の配置は引用符なし（PowerShell でも & なしで貼れる）
    assert commands.make_cert == (
        r"C:\LinguaBridge\app\python\python.exe C:\LinguaBridge\app\scripts\make_cert.py"
    )
    assert commands.diagnostics == r"C:\LinguaBridge\app\python\python.exe -m server.diagnostics"


def test_distribution_paths_with_spaces_are_quoted():
    app = Path(r"D:\Lingua Bridge\app")

    commands = launch_commands(r"D:\Lingua Bridge\app\python\python.exe", app)

    assert commands.make_cert == (
        r'"D:\Lingua Bridge\app\python\python.exe" "D:\Lingua Bridge\app\scripts\make_cert.py"'
    )
    assert commands.diagnostics == r'"D:\Lingua Bridge\app\python\python.exe" -m server.diagnostics'


def test_bundled_python_is_recognized_regardless_of_case():
    app = Path(r"C:\LinguaBridge\app")

    commands = launch_commands(r"c:\linguabridge\APP\python\python.exe", app)

    assert ".venv" not in commands.diagnostics


def test_other_interpreters_keep_the_development_commands():
    app = Path(r"C:\repo")

    commands = launch_commands(r"C:\repo\.venv\Scripts\python.exe", app)

    assert commands.make_cert == r".venv\Scripts\python scripts\make_cert.py"
    assert commands.diagnostics == r".venv\Scripts\python -m server.diagnostics"
