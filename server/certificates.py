"""接続先確定後に行う証明書の読み取り検証。通信・書き換えはしない。"""

from __future__ import annotations

import ssl
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.config import ServerConfig


def inspect_certificate(config: ServerConfig, ip: str | None) -> dict:
    result: dict = {
        "certificate": {"status": "unknown", "detail": "接続先未確定のため証明書未確認"},
        "key_pair": {"status": "unknown", "detail": "接続先未確定のため鍵未確認"},
        "remote_trust": {"status": "unknown", "detail": "別端末の信頼・マイク・字幕は未確認"},
    }
    if ip is None:
        return result
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes

        cert = x509.load_pem_x509_certificate(config.cert_path().read_bytes())
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            addresses = [str(value) for value in san.get_values_for_type(x509.IPAddress)]
        except x509.ExtensionNotFound:
            addresses = []
        now = datetime.now(timezone.utc)
        issues = []
        if ip not in addresses:
            issues.append(f"SAN不一致（採用IP {ip}）")
        if now < cert.not_valid_before_utc:
            issues.append("有効期間前")
        if now >= cert.not_valid_after_utc:
            issues.append("期限切れ")
        result["certificate"] = {
            "status": "failed" if issues else "ok",
            "detail": " / ".join(issues) if issues else "採用IPとSAN一致・有効期間内",
            "ip_addresses": addresses,
            "not_before": cert.not_valid_before_utc.isoformat(),
            "expires": cert.not_valid_after_utc.isoformat(),
            "days_remaining": (cert.not_valid_after_utc - now).days,
            "sha256": cert.fingerprint(hashes.SHA256()).hex(),
            "checked_at": now.isoformat(),
        }
    except FileNotFoundError:
        result["certificate"] = {"status": "failed", "detail": "証明書なし"}
    except ValueError:
        result["certificate"] = {"status": "failed", "detail": "証明書の形式を確認できません"}
    except (OSError, ImportError) as exc:
        result["certificate"] = {"status": "unknown", "detail": f"証明書未確認: {type(exc).__name__}"}
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(config.cert_path()), str(config.key_path()), password="")
        result["key_pair"] = {"status": "ok", "detail": "証明書と秘密鍵が一致"}
    except FileNotFoundError:
        result["key_pair"] = {"status": "failed", "detail": "証明書または秘密鍵なし"}
    except ssl.SSLError:
        result["key_pair"] = {"status": "failed", "detail": "鍵不整合または証明書・鍵の形式不正"}
    except OSError as exc:
        result["key_pair"] = {"status": "unknown", "detail": f"鍵未確認: {type(exc).__name__}"}
    return result


def certificate_ready(report: dict) -> bool:
    return all(report[name]["status"] == "ok" for name in ("certificate", "key_pair"))


def print_certificate_report(report: dict) -> None:
    for label, name in (("証明書", "certificate"), ("鍵", "key_pair")):
        item = report[name]
        print(f"  TLS {label}: {item['status']} / {item['detail']}")
    cert = report["certificate"]
    if "sha256" in cert:
        print(f"  SAN IP: {', '.join(cert['ip_addresses'])}")
        print(f"  有効期間: {cert['not_before']} ～ {cert['expires']}")
        print(f"  検証時刻: {cert['checked_at']} / SHA256: {cert['sha256']}")
    print(f"  {report['remote_trust']['detail']}（警告承認と授業機能の成功は別判定）")
