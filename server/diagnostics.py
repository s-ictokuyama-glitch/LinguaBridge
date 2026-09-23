"""起動とは独立した読み取り診断。OS設定・モデル・参加セッションは変更しない。"""

from __future__ import annotations

import argparse
import hashlib
import http.client
from ipaddress import IPv4Address
import json
import platform
import socket
import ssl
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import yaml

from server.config import AppConfig, ServerConfig, load_config
from server.certificates import inspect_certificate
from dataclasses import asdict

from server import network as network_addresses


# 担当者が端末ごとに記入する欄。手順書・ヘルプページ・--json の記録欄はこの一覧から作る。
RECORD_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("timestamp", "時刻（タイムゾーン）", ""),
    ("config_file", "診断に使用した設定ファイル", ""),
    ("device_os_version", "端末・OSの版", ""),
    ("browser_version", "ブラウザの版（取得不能なら未確認）", ""),
    ("requested_url_without_code", "入力したURL（参加コードを伏せる）", ""),
    ("final_url_without_code", "ブラウザの最終URL（参加コードを伏せる）", ""),
    ("server_http", "サーバーPCのHTTP", "未確認"),
    ("remote_http", "別端末のHTTP", "未確認"),
    ("remote_tls", "別端末のTLS", "未確認"),
    ("certificate_warning", "証明書警告・承認可否", "未確認"),
    ("remote_page", "別端末のページ取得", "未確認"),
    ("remote_ws_join", "別端末のWS参加", "未確認"),
    ("mic_captions", "マイク・字幕", "未確認"),
    ("firewall_repair", "FW修復の要否・担当者", "未確認"),
    ("actual_error_without_code", "実際のエラー（参加コードを伏せる）", ""),
    ("matching_log_without_code", "同じ時刻の対応ログ（参加コードを伏せる／無い・取得不能も明記）", ""),
    ("last_successful_stage", "最後に成功した段階", ""),
    ("next_stage", "次に試す段階・担当者", ""),
)


def record_template() -> str:
    return "\n".join(f"{label}: {default}".rstrip() for _, label, default in RECORD_FIELDS)


def check(status: str, detail: str, **values) -> dict:
    return {"status": status, "detail": detail, **values}


def probe_http(host: str, port: int, path: str) -> dict:
    # http.client は環境のプロキシを使わず、リダイレクトも追跡しない。
    url = f"http://{host}:{port}{path}"
    connection = http.client.HTTPConnection(host, port, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read(65536)
        if path in ("/healthz", "/ready"):
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                payload = None
            expected = isinstance(payload, dict) and payload.get("status") == "ok"
        else:
            expected = b"LinguaBridge" in body
        status = "ok" if response.status == 200 and expected else "failed"
        detail = f"HTTP {response.status}"
        if response.status == 200 and not expected:
            detail += "（期待するアプリ応答ではありません）"
        if path == "/ready" and response.status == 503:
            detail += "（モデル未準備。通信断とは別です）"
        return check(status, detail, url=url, http_status=response.status)
    except (PermissionError, TimeoutError) as exc:
        return check("unknown", type(exc).__name__, url=url)
    except (OSError, http.client.HTTPException) as exc:
        return check("failed", type(exc).__name__, url=url)
    finally:
        connection.close()


def inspect_windows(config: ServerConfig) -> dict:
    unavailable = {
        name: check("unknown", "Windows情報を取得できません")
        for name in ("interfaces", "listeners", "firewall")
    }
    if platform.system() != "Windows":
        return unavailable
    script = Path(__file__).resolve().parent.parent / "scripts" / "diagnose_windows.ps1"
    output = ""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-HttpPort", str(config.http_port), "-HttpsPort", str(config.https_port)],
            capture_output=True, encoding="utf-8", timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = result.stdout
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"").decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError, ValueError):
        pass  # stderrにはOSやポリシーの詳細が含まれるため収集しない。
    for line in output.splitlines():
        try:
            data = json.loads(line.lstrip("\ufeff"))
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        for name in unavailable:
            item = data.get(name)
            if isinstance(item, dict) and item.get("status") in ("observed", "unknown"):
                unavailable[name] = item
    return unavailable


def inspect_tls(config: ServerConfig, ip: str | None) -> dict:
    result = inspect_certificate(config, ip)
    result["served_certificate"] = check("unknown", "待受TLS未確認")
    result["https_health"] = check("unknown", "HTTPS応答未確認")
    fingerprint = result["certificate"].get("sha256")
    if fingerprint and ip:
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.check_hostname = False
        client_context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(ip, config.https_port, timeout=2, context=client_context)
        try:
            # プロキシ・リダイレクトなし。同じTLS接続で証明書と死活確認を検証する。
            # CERT_NONE はこの診断専用で、ブラウザの発行元信頼成功とは扱わない。
            connection.connect()
            assert isinstance(connection.sock, ssl.SSLSocket)
            served = hashlib.sha256(connection.sock.getpeercert(binary_form=True) or b"").hexdigest()
            result["served_certificate"] = check(
                "ok" if served == fingerprint else "failed",
                "提供中証明書と設定ファイルのSHA256比較（信頼検証とは別）", sha256=served,
            )
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            try:
                payload = json.loads(response.read(65536))
            except (ValueError, UnicodeError):
                payload = None
            expected = isinstance(payload, dict) and payload.get("status") == "ok"
            result["https_health"] = check(
                "ok" if response.status == 200 and expected else "failed",
                f"HTTPS {response.status} / アプリ応答一致={expected}（別端末・マイク・字幕は未確認）",
                url=f"https://{ip}:{config.https_port}/healthz", http_status=response.status,
            )
        except (PermissionError, TimeoutError) as exc:
            result["https_health"] = check("unknown", type(exc).__name__)
            if result["served_certificate"]["status"] == "unknown":
                result["served_certificate"] = check("unknown", type(exc).__name__)
        except (OSError, http.client.HTTPException) as exc:
            result["https_health"] = check("failed", type(exc).__name__)
            if result["served_certificate"]["status"] == "unknown":
                result["served_certificate"] = check("failed", type(exc).__name__)
        finally:
            connection.close()
    return result


def diagnose(config: AppConfig, config_file: str = "") -> dict:
    """JSON化できる観測結果のみを返す。レスポンス本文や秘密情報は収集しない。"""
    addresses = network_addresses.list_addresses()
    ip = None
    try:
        ip = network_addresses.select_address(addresses, config.server.advertise_ip).ip
        network = check("observed", "起動時と同じNIC状態・役割・明示指定による選択")
    except ValueError as exc:
        network = check("unknown", str(exc))
    network.update(
        candidates=[item.ip for item in addresses], selected_ip=ip,
        interfaces=[asdict(item) for item in addresses],
        requested_ip=config.server.advertise_ip, usable_for_remote=ip is not None,
    )
    os_version = sys.platform
    if sys.platform == "win32":
        windows_version = sys.getwindowsversion()
        os_version = f"Windows {windows_version.major}.{windows_version.minor}.{windows_version.build}"
    runtime = {"python": platform.python_version(), "os": os_version}
    for package in ("fastapi", "uvicorn", "cryptography"):
        try:
            runtime[package] = version(package)
        except PackageNotFoundError:
            runtime[package] = "未確認（未インストール）"
    port = config.server.http_port
    windows = inspect_windows(config.server)
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ports": {"http": port, "https": config.server.https_port},
        "network": network,
        "runtime": runtime,
        **windows,
        "tls": inspect_tls(config.server, ip),
        "probes": {
            "loopback_http": probe_http("127.0.0.1", port, "/healthz"),
            "selected_ip_http": (probe_http(ip, port, "/healthz") if ip else
                                 check("unknown", "公開IP未選択のため未実施")),
            "models": probe_http("127.0.0.1", port, "/ready"),
            "page": (probe_http(ip, port, "/") if ip else
                     check("unknown", "公開IP未選択のため未実施")),
        },
        "remote": {
            stage: check("unknown", "別端末で未実施。サーバーPCの成功では確認できません")
            for stage in ("http", "tls", "page", "ws_join")
        },
        "record": {key: default for key, _, default in RECORD_FIELDS} | {"config_file": config_file},
    }
    report["next_steps"] = next_steps(report)
    return report


def next_steps(report: dict) -> list[str]:
    ip = report["network"]["selected_ip"]
    http, https = report["ports"]["http"], report["ports"]["https"]
    if not report["network"]["usable_for_remote"]:
        return [
            f"採用IP（{ip or '未確認'}）は別端末用の接続先として案内できません。候補の取得状況と実Wi-FiのIPv4を担当者が確認してください。",
            f"サーバーPC内の死活確認は http://127.0.0.1:{http}/healthz、モデル準備は /ready で比較してください。",
            "別端末のHTTP・TLS・ページ取得・WS参加は未確認です。ping失敗やログ不在だけでAP分離と断定しません。",
            "時刻・端末/ブラウザの版・最終URL・実際のエラー・対応ログ・次の段階を docs/connection-diagnostics.md の記録欄へ記入し、参加コードを伏せてください。秘密鍵は収集しません。",
        ]
    steps = []
    if report["probes"]["loopback_http"]["status"] != "ok":
        steps.append("最初にサーバーの起動ログと設定ポート・待受PIDを確認してください。取得不能は未確認です。")
    elif report["probes"]["selected_ip_http"]["status"] != "ok":
        steps.append("ループバックは成功しています。採用IPと実Wi-Fi、待受アドレス、サーバーPCのファイアウォールを照合してください。")
    else:
        steps.append("サーバーPCでHTTP応答を確認しました。次は既存の社内Wi-Fiにつないだ別端末で比較します。")
    if any(report["tls"][name]["status"] == "failed" for name in ("certificate", "key_pair")):
        # 未確認は不整合と断定しない。失敗を観測した場合だけ再発行へ誘導する。
        config_file = report["record"]["config_file"] or "<設定ファイル>"
        steps.append(
            "証明書のIP・期限・鍵に不整合があります。サーバー停止後、同じ設定で "
            f".venv\\Scripts\\python scripts\\make_cert.py --config {config_file} --advertise-ip {ip} --force を実行し、"
            "再起動後に同じ診断で再確認してください。手順と復元: docs/certificate-recovery.md"
        )
    steps.extend([
        f"サーバーPCで http://127.0.0.1:{http}/healthz を開き、同PCと別端末で http://{ip}:{http}/healthz を比較してください。",
        f"http://{ip}:{http}/ready の503はモデル未準備です。/healthzの死活確認と分けて起動ログを確認してください。",
        f"HTTPSを使う別端末では https://{ip}:{https}/healthz を確認してください。証明書の整合性と別端末の信頼は別判定です。",
        f"http://{ip}:{http}/ または https://{ip}:{https}/teacher を開き、ブラウザの最終URL（スキーム・IP・ポート・パス）を記録してください。",
        "ページ取得後、参加操作と先生ページの接続人数でWS参加を確認してください。時刻を対応ログと照合します。",
        "ping失敗やログ不在だけでAP分離と断定しません。HTTP・TLS・ページ取得・WS参加の未確認を区別してください。",
        "時刻・端末/OSの版・ブラウザの版・入力URL/最終URL・実際のエラー・対応ログ・次の段階を端末ごとに記録してください。",
        "参加コードはURL・エラー・ログ・画面から伏せ、秘密鍵は収集・添付しないでください。",
        "復旧手順と記録用テンプレート: docs/connection-diagnostics.md（変更対象はアプリとサーバーPC。現地操作は後日担当者と協働）",
    ])
    return steps


def print_report(report: dict) -> None:
    labels = {"ok": "確認済み", "failed": "失敗", "observed": "取得済み", "unknown": "未確認"}

    def show(name: str, result: dict) -> None:
        print(f"  {name}: {labels[result['status']]} - {result['detail']}")

    print(f"LinguaBridge 接続診断（読み取り専用） {report['timestamp']}")
    print("診断はOS設定・モデル・参加状態を変更しません。取得済みは接続成功の意味ではありません。")
    show("ネットワーク", report["network"])
    print(f"  IPv4候補: {', '.join(report['network']['candidates']) or '未確認'}")
    print(f"  採用IP: {report['network']['selected_ip'] or '未確認'}（起動時と同じ規則。実Wi-Fiと要照合）")
    print(f"  設定ポート: HTTP={report['ports']['http']} / HTTPS={report['ports']['https']}")
    print(f"  診断ランタイム: {json.dumps(report['runtime'], ensure_ascii=False)}")
    for name, key in (("アダプター", "interfaces"), ("待受プロセス", "listeners"), ("ファイアウォール", "firewall")):
        show(name, report[key])
        for item in report[key].get("items", []):
            print(f"    {json.dumps(item, ensure_ascii=False)}")
    firewall = report["firewall"]
    if firewall.get("inventory_status") == "observed":
        print(f"    適用候補ルール: {len(firewall.get('rules', []))}件（制限詳細は --json）")
    for profile in firewall.get("profiles", []):
        print(f"    {json.dumps(profile, ensure_ascii=False)}")
    for name, key in (("ループバックHTTP", "loopback_http"), ("採用IPのHTTP", "selected_ip_http"), ("モデル準備", "models"), ("ページ取得", "page")):
        show(name, report["probes"][key])
    for name, key in (("TLS証明書/IP/期限", "certificate"), ("TLS鍵の一致", "key_pair"), ("TLS提供中証明書", "served_certificate"), ("HTTPS死活確認", "https_health"), ("別端末のTLS信頼", "remote_trust")):
        show(name, report["tls"][key])
    print("  別端末: HTTP=未確認 / TLS=未確認 / ページ取得=未確認 / WS参加=未確認")
    print("\n次の操作:")
    for step in report["next_steps"]:
        print(f"  - {step}")
    print("\n担当者向け詳細・空の記録欄は --json で取得できます（OS情報を含むため担当者間で管理）。")


def main(argv: list[str] | None = None) -> int:
    # Windowsのリダイレクト先(cp932等)でOS由来の文字により診断全体を落とさない。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--advertise-ip", help="起動で一時指定・選択した公開IPv4")
    parser.add_argument("--json", action="store_true", help="診断結果と空の記録欄をJSON出力")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.advertise_ip is not None:
            config.server.advertise_ip = str(IPv4Address(args.advertise_ip))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"設定: 未確認（{type(exc).__name__}）。--config のファイルを確認してください。")
        return 2
    report = diagnose(config, config_file=args.config)
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
