"""起動とは独立した読み取り診断。OS設定・モデル・参加セッションは変更しない。"""

from __future__ import annotations

import argparse
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
from dataclasses import asdict

from server import network as network_addresses


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
    result = {
        "certificate": check("unknown", "証明書未確認"),
        "key_pair": check("unknown", "鍵との整合性未確認"),
        "served_certificate": check("unknown", "待受TLS未確認"),
        "remote_trust": check("unknown", "別端末の証明書信頼・ブラウザ制限は未確認"),
    }
    fingerprint = None
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes

        cert = x509.load_pem_x509_certificate(config.cert_path().read_bytes())
        fingerprint = cert.fingerprint(hashes.SHA256()).hex()
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            addresses = [str(address) for address in san.get_values_for_type(x509.IPAddress)]
        except x509.ExtensionNotFound:
            addresses = []
        now = datetime.now(timezone.utc)
        valid = cert.not_valid_before_utc <= now <= cert.not_valid_after_utc
        matches = ip in addresses
        result["certificate"] = check(
            "ok" if valid and matches else "failed",
            f"有効期間内={valid} / 採用IPとSAN一致={matches}",
            ip_addresses=addresses, expires=cert.not_valid_after_utc.isoformat(),
            sha256=fingerprint,
        )
    except (OSError, ValueError, ImportError) as exc:
        result["certificate"] = check("unknown", type(exc).__name__)
    try:
        # 鍵はSSLライブラリがローカルで照合するだけ。内容を出力・保存しない。
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(config.cert_path()), str(config.key_path()), password="")
        result["key_pair"] = check("ok", "設定された証明書と秘密鍵が一致")
    except ssl.SSLError:
        result["key_pair"] = check("failed", "証明書と秘密鍵を読み込めません（不一致・形式を確認）")
    except OSError as exc:
        result["key_pair"] = check("unknown", type(exc).__name__)
    if fingerprint and ip:
        try:
            # この接続は提供中証明書の比較専用。信頼成功とは扱わずHTTPも送らない。
            client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_context.check_hostname = False
            client_context.verify_mode = ssl.CERT_NONE
            with socket.create_connection((ip, config.https_port), timeout=2) as raw:
                with client_context.wrap_socket(raw, server_hostname=ip) as connection:
                    import hashlib

                    served = hashlib.sha256(connection.getpeercert(binary_form=True) or b"").hexdigest()
                    result["served_certificate"] = check(
                        "ok" if served == fingerprint else "failed",
                        "提供中証明書と設定ファイルのSHA256比較（信頼検証とは別）",
                        sha256=served,
                    )
        except (PermissionError, TimeoutError) as exc:
            result["served_certificate"] = check("unknown", type(exc).__name__)
        except OSError as exc:
            result["served_certificate"] = check("failed", type(exc).__name__)
    return result


def diagnose(config: AppConfig) -> dict:
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
        "record": {
            "timestamp": "", "device_os_version": "", "browser_version": "",
            "requested_url_without_code": "", "final_url_without_code": "",
            "actual_error_without_code": "", "matching_log_without_code": "",
            "http": "未確認", "tls": "未確認", "page": "未確認",
            "ws_join": "未確認", "next_stage": "",
        },
    }
    report["next_steps"] = next_steps(report)
    return report


def next_steps(report: dict) -> list[str]:
    ip = report["network"]["selected_ip"]
    http, https = report["ports"]["http"], report["ports"]["https"]
    if not report["network"]["usable_for_remote"]:
        return [
            f"採用IP {ip} は別端末用の接続先として案内できません。候補の取得状況と実Wi-FiのIPv4を担当者が確認してください。",
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
    print(f"  採用IP: {report['network']['selected_ip']}（起動時と同じ規則。実Wi-Fiと要照合）")
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
    for name, key in (("TLS証明書/IP/期限", "certificate"), ("TLS鍵の一致", "key_pair"), ("TLS提供中証明書", "served_certificate"), ("別端末のTLS信頼", "remote_trust")):
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
    report = diagnose(config)
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
