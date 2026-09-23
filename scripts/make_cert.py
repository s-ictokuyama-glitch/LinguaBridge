"""自己署名TLS証明書の生成（イシュー#16 / plan.md E-15）。

先生ページのHTTPS（マイクのセキュアコンテキスト）用。SANに localhost・127.0.0.1・
このPCのLAN IP・ホスト名を入れる。有効期間は825日（Apple等のTLS上限に合わせる）。
出力: certs/cert.pem, certs/key.pem（config.server.cert_dir）。

    python scripts/make_cert.py            # config.yaml の cert_dir に生成
    python scripts/make_cert.py --force    # サーバー停止後、旧ペアを退避して再発行
    python scripts/make_cert.py --restore <退避先>  # 退避した一組を復元
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import hashlib
import json
import os
import socket
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import ServerConfig, load_config  # noqa: E402
from server.certificates import certificate_ready, inspect_certificate, print_certificate_report  # noqa: E402
from server.main import get_lan_ip  # noqa: E402
from server.network import choose_ip  # noqa: E402

VALID_DAYS = 825
# 既存ファイルを変更せず、正常とも確認できなかった。setup.ps1 は警告として続行する。
# argparse の引数エラー（2）と区別するため3。
EXIT_UNCHANGED_NOT_READY = 3


def build_san(ip: str) -> list:
    from cryptography import x509

    names: list = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    hostname = socket.gethostname()
    if hostname:
        names.append(x509.DNSName(hostname))
    try:
        names.append(x509.IPAddress(ipaddress.ip_address(ip)))
    except ValueError:
        pass
    return names


def generate(cert_path: Path, key_path: Path, *, ip: str | None = None) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "LinguaBridge")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(build_san(ip or get_lan_ip())), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _read_pair(paths: tuple[Path, Path]) -> list[bytes | None]:
    values: list[bytes | None] = []
    for path in paths:
        try:
            values.append(path.read_bytes())
        except FileNotFoundError:
            values.append(None)
    return values


def _backup_pair(paths: tuple[Path, Path], values: list[bytes | None]) -> Path:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = paths[0].parent / "backups" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    backup.mkdir(parents=True, mode=0o700)
    entries = []
    for name, path, value in zip(("cert.pem", "key.pem"), paths, values):
        if value is not None:
            (backup / name).write_bytes(value)
            (backup / name).chmod(0o600)
        entries.append({"target": str(path.resolve()),
                        "sha256": hashlib.sha256(value).hexdigest() if value is not None else None})
    # 最後に書く。manifest のない途中の退避は復元操作で受け付けない。
    (backup / "manifest.json").write_text(json.dumps(entries), encoding="utf-8")
    print(f"旧証明書・鍵の退避先: {backup}")
    print(f'復元: 同じ --config と --advertise-ip に --restore "{backup}" を指定')
    return backup


def _restore_values(backup: Path, paths: tuple[Path, Path]) -> list[bytes | None]:
    entries = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(entries, list) or len(entries) != 2:
        raise ValueError("退避した一組の記録が不正です")
    values = _read_pair((backup / "cert.pem", backup / "key.pem"))
    for entry, path, value in zip(entries, paths, values):
        digest = hashlib.sha256(value).hexdigest() if value is not None else None
        if not isinstance(entry, dict) or "sha256" not in entry or entry.get("target") != str(path.resolve()) or entry["sha256"] != digest:
            raise ValueError("退避した一組の内容または復元先が一致しません")
    return values


def _write_pair(paths: tuple[Path, Path], values: list[bytes | None]) -> None:
    for path, value in zip(paths, values):
        if value is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(prefix=".certificate-", dir=path.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def replace_pair(cert_path: Path, key_path: Path, *, ip: str, restore: Path | None) -> None:
    """停止中のサーバー用。両ファイルを退避してから置換し、例外時は一組を戻す。"""
    if cert_path.resolve() == key_path.resolve():
        raise ValueError("証明書と鍵には別のファイルを指定してください")
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    paths = (cert_path, key_path)
    lock = cert_path.parent / ".certificate-operation.lock"
    with lock.open("x"):
        pass
    try:
        original = _read_pair(paths)
        if restore is not None:
            replacement = _restore_values(restore, paths)
        else:
            with tempfile.TemporaryDirectory(prefix=".certificate-", dir=cert_path.parent) as directory:
                staging = Path(directory)
                generate(staging / "cert.pem", staging / "key.pem", ip=ip)
                if not certificate_ready(inspect_certificate(ServerConfig(cert_dir=str(staging)), ip)):
                    raise ValueError("生成した証明書の検証に失敗しました。旧ファイルは変更していません")
                replacement = _read_pair((staging / "cert.pem", staging / "key.pem"))
        backup = _backup_pair(paths, original)
        _restore_values(backup, paths)  # 退避した一組を読めることを確かめてから置換する。
        try:
            _write_pair(paths, replacement)
        except BaseException:
            try:
                _write_pair(paths, original)
            except OSError as exc:
                raise OSError(f"自動復元できません。サーバーを停止したまま --restore \"{backup}\" を実行してください") from exc
            raise
    finally:
        lock.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--force", action="store_true", help="旧証明書・鍵を退避して再発行する（サーバー停止後）")
    operation.add_argument("--restore", type=Path, help="退避ディレクトリの一組を復元する（サーバー停止後）")
    parser.add_argument("--advertise-ip", help="起動と同じ公開IPv4（今回のみ）")
    parser.add_argument("--select-network", action="store_true", help="曖昧な接続先を対話選択")
    args = parser.parse_args()

    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("cryptography が必要です: .venv\\Scripts\\pip install cryptography")
        return 1

    config = load_config(args.config)
    cert_path = config.server.cert_path()  # config 側でリポジトリルート基準に解決済み
    key_path = config.server.key_path()
    keep_existing = not args.force and args.restore is None and (cert_path.exists() or key_path.exists())
    try:
        ip: str | None = choose_ip(args.advertise_ip or config.server.advertise_ip,
                                   interactive=args.select_network)
    except ValueError as exc:
        if not keep_existing or args.advertise_ip:  # 明示したIPの誤りは入力ミスとして失敗
            print(f"証明書を生成できません: {exc}")
            return 1
        print(f"接続先未確定のため既存の証明書は未確認: {exc}")
        ip = None
    report = inspect_certificate(config.server, ip)
    print_certificate_report(report)
    if keep_existing or ip is None:  # ip が None になるのは keep_existing のときだけ（型の絞り込み）
        print("既存ファイルは変更していません。再発行はサーバー停止後に同じ接続先で --force。")
        return 0 if certificate_ready(report) else EXIT_UNCHANGED_NOT_READY
    try:
        replace_pair(cert_path, key_path, ip=ip, restore=args.restore)
    except (OSError, ValueError) as exc:
        print(f"証明書の変更を完了できません: {exc}")
        return 1
    print(f"照合対象の公開IP: {ip}")
    print(f"{'復元' if args.restore else '生成'}しました:\n  {cert_path}\n  {key_path}")
    print_certificate_report(inspect_certificate(config.server, ip))
    print("サーバーを同じ接続先で再起動し、HTTPSを再確認してください。")
    print(f'  .venv\\Scripts\\python -m server.diagnostics --config "{args.config}" --advertise-ip {ip} --json')
    print(f"  https://{ip}:{config.server.https_port}/healthz")
    print("警告承認・復元手順: docs/certificate-recovery.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
