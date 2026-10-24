"""公開するIPv4の決定。OSのローカル情報だけを使い、外部接続しない。"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address
import json
from pathlib import Path
import platform
import socket
import subprocess

import psutil  # type: ignore[import-untyped]


@dataclass(frozen=True)
class InterfaceAddress:
    name: str
    ip: str
    up: bool
    role: str  # physical / virtual / unknown

    @property
    def usable(self) -> bool:
        try:
            address = IPv4Address(self.ip)
        except ValueError:
            return False
        return self.up and not (
            address.is_loopback or address.is_link_local or address.is_unspecified
            or address.is_multicast or address.is_reserved
        )


def select_address(
    addresses: list[InterfaceAddress], requested_ip: str | None = None,
) -> InterfaceAddress:
    if requested_ip is not None:
        matches = [item for item in addresses if item.usable and item.ip == requested_ip]
        if len(matches) == 1:
            return matches[0]
        raise ValueError(f"指定IP {requested_ip} が有効なNICに一意に存在しません。再選択してください。")
    physical = [item for item in addresses if item.usable and item.role == "physical"]
    if len(physical) == 1:
        return physical[0]
    raise ValueError("利用中のネットワークを選択してください（--select-network または server.advertise_ip）。")


def list_addresses() -> list[InterfaceAddress]:
    """Windowsは物理NICフラグとアドレス状態を取得。取得不能を推測で補わない。"""
    if platform.system() == "Windows":
        script = Path(__file__).resolve().parent.parent / "scripts" / "network_interfaces.ps1"
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(script)],
                capture_output=True, encoding="utf-8", timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True,
            )
            return [InterfaceAddress(**item) for item in json.loads(result.stdout.lstrip("\ufeff"))]
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            # ポリシー等でCIMを読めなくても稼働状態を取得できれば明示選択は可能。
            pass
    try:
        stats = psutil.net_if_stats()
        return [
            InterfaceAddress(name, address.address, stats[name].isup, "unknown")
            for name, addresses in psutil.net_if_addrs().items() if name in stats
            for address in addresses if address.family == socket.AF_INET
        ]
    except OSError:
        return []


def resolve_ip(requested_ip: str | None = None) -> str:
    return select_address(list_addresses(), requested_ip).ip


def choose_address(requested_ip: str | None, *, interactive: bool) -> InterfaceAddress:
    """起動と証明書CLIで共有する選択。入力値は候補番号のみ、設定は書き換えない。"""
    addresses = list_addresses()
    try:
        return select_address(addresses, requested_ip)
    except ValueError as exc:
        candidates = [item for item in addresses if item.usable]
        if not interactive:
            raise
        print(str(exc))
        for number, item in enumerate(candidates, 1):
            print(f"  {number}: {item.name} / {item.ip} / {item.role}")
        if not candidates:
            raise
        try:
            number = int(input("利用中の社内Wi-Fiの番号（中止はEnter）: "))
            if not 1 <= number <= len(candidates):
                raise ValueError()
        except (ValueError, EOFError) as error:
            raise ValueError("接続先を選択せずに中止しました。") from error
        # 入力を待つ間に切断された場合も、古いスナップショットで決定しない。
        return select_address(list_addresses(), candidates[number - 1].ip)


def choose_ip(requested_ip: str | None, *, interactive: bool) -> str:
    return choose_address(requested_ip, interactive=interactive).ip
