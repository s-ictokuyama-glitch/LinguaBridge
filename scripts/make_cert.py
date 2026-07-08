"""自己署名TLS証明書の生成（イシュー#16 / plan.md E-15）。

先生ページのHTTPS（マイクのセキュアコンテキスト）用。SANに localhost・127.0.0.1・
このPCのLAN IP・ホスト名を入れる。有効期間は825日（Apple等のTLS上限に合わせる）。
出力: certs/cert.pem, certs/key.pem（config.server.cert_dir）。

    python scripts/make_cert.py            # config.yaml の cert_dir に生成
    python scripts/make_cert.py --force    # 既存を上書き再生成
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import load_config  # noqa: E402
from server.main import get_lan_ip  # noqa: E402

VALID_DAYS = 825


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


def generate(cert_path: Path, key_path: Path) -> None:
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
        .add_extension(x509.SubjectAlternativeName(build_san(get_lan_ip())), critical=False)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--force", action="store_true", help="既存の証明書を上書きする")
    args = parser.parse_args()

    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("cryptography が必要です: .venv\\Scripts\\pip install cryptography")
        return 1

    config = load_config(args.config)
    cert_path = config.server.cert_path()  # config 側でリポジトリルート基準に解決済み
    key_path = config.server.key_path()
    if cert_path.exists() and key_path.exists() and not args.force:
        print(f"証明書は既に存在します: {cert_path}（再生成は --force）")
        return 0

    generate(cert_path, key_path)
    print(f"生成しました:\n  {cert_path}\n  {key_path}")
    print(f"有効期間: {VALID_DAYS}日。先生ページを HTTPS で開くと初回に警告が出るので承認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
