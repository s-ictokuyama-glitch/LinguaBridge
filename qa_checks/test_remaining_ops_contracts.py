"""OPS-03 / OPS-13 / OPS-14 の未充足運用契約を可視化する。

ここの反例はOSのファイアウォールや電源設定を変更せず、出荷コードと
運用文書の契約だけを検査する。製品側が未修正の間は反例5件が失敗するのが
意図した結果である。
"""

from __future__ import annotations

import asyncio
import webbrowser
from pathlib import Path

import pytest

from server.main import _open_teacher_page_when_ready
from server.network import InterfaceAddress, select_address


ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = ROOT / "setup.ps1"
README = ROOT / "README.md"


class _Readiness:
    def __init__(self, ready: bool = False) -> None:
        self.ready = ready


def test_ops03_teacher_page_never_opens_before_pipeline_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPS-03: 待機上限へ達しても、未準備の画面を開かない。"""
    pipeline = _Readiness()
    opened: list[str] = []

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    monkeypatch.setattr(webbrowser, "open", opened.append)

    asyncio.run(_open_teacher_page_when_ready(pipeline, "http://127.0.0.1/teacher"))

    assert opened == [], "pipeline.ready=False のまま先生ページが開かれた"


def test_ops03_teacher_page_opens_once_when_pipeline_becomes_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPS-03: ready遷移後は対象URLを1回だけ開く。"""
    pipeline = _Readiness()
    opened: list[str] = []
    sleeps = 0

    async def become_ready(_: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            pipeline.ready = True

    monkeypatch.setattr(asyncio, "sleep", become_ready)
    monkeypatch.setattr(webbrowser, "open", opened.append)

    url = "http://127.0.0.1/teacher"
    asyncio.run(_open_teacher_page_when_ready(pipeline, url))

    assert sleeps == 3
    assert opened == [url]


def test_ops13_restart_after_hotspot_switch_publishes_new_hotspot_ip() -> None:
    """OPS-13: 旧Wi-Fiが残っても再起動後のQRはhotspot側IPを使う。"""
    previous_wifi_ip = "192.168.1.34"
    hotspot_ip = "192.168.137.1"

    # 公開IPは #38 以降、NICの稼働状態と役割（物理・仮想）から選ぶ。Windowsのホットスポットは仮想アダプターとして見える
    selected = select_address([
        InterfaceAddress("Wi-Fi", previous_wifi_ip, True, "physical"),
        InterfaceAddress("ローカル エリア接続* 10", hotspot_ip, True, "virtual"),
    ]).ip
    published_url = f"http://{selected}:8000/?code=4831"

    assert published_url == f"http://{hotspot_ip}:8000/?code=4831", (
        "ホットスポット切替後も旧Wi-Fi側IPが参加URLに残った"
    )


def _unchecked_powercfg_commands(source: str) -> list[str]:
    commands = [
        "powercfg /change standby-timeout-ac 0",
        "powercfg /change hibernate-timeout-ac 0",
    ]
    unchecked: list[str] = []
    completion = source.index('Section "完了"')

    for index, command in enumerate(commands):
        start = source.index(command) + len(command)
        end = source.index(commands[index + 1]) if index + 1 < len(commands) else completion
        check = source[start:end]
        if "Assert-ExitOk" not in check and "$LASTEXITCODE" not in check:
            unchecked.append(command)
    return unchecked


def test_ops14_powercfg_failures_prevent_setup_completion_marker() -> None:
    """OPS-14: 両powercfgの失敗を検出してからでなければ完了に進まない。"""
    source = SETUP_SCRIPT.read_text(encoding="utf-8")

    unchecked = _unchecked_powercfg_commands(source)

    assert unchecked == [], f"終了コード未確認のnative command: {unchecked}"


def test_ops14_existing_firewall_rule_is_revalidated_before_accepting_it() -> None:
    """OPS-14: 同名規則の存在だけでなく、有効性とport filterを確認する。"""
    source = SETUP_SCRIPT.read_text(encoding="utf-8")
    required_contracts = {
        "Get-NetFirewallPortFilter": "ポートフィルタの取得",
        ".Enabled": "規則の有効/無効",
        ".Direction": "受信方向",
        ".Action": "Allow action",
        ".Protocol": "TCP protocol",
        ".LocalPort": "設定ポート",
    }
    missing = [
        description
        for token, description in required_contracts.items()
        if token not in source
    ]

    assert missing == [], f"既存ファイアウォール規則の内容検証が不足: {missing}"


def test_ops14_non_admin_docs_include_concrete_firewall_and_power_steps() -> None:
    """OPS-14: 非管理者に、後で実行できる具体的な手動手順を渡す。"""
    readme = README.read_text(encoding="utf-8")
    required_steps = {
        "New-NetFirewallRule": "受信TCPポートの許可コマンド",
        "powercfg /change standby-timeout-ac 0": "AC時スリープ無効化",
        "powercfg /change hibernate-timeout-ac 0": "AC時休止無効化",
    }
    missing = [
        description
        for command, description in required_steps.items()
        if command not in readme
    ]

    assert missing == [], f"非管理者向けの具体的な手動手順が不足: {missing}"
