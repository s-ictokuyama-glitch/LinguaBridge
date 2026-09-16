"""#40: 確定した接続先に対する起動案内と証明書CLI。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from starlette.testclient import TestClient

from scripts import make_cert
from server import main
from server.config import AppConfig, ServerConfig


IP = "192.168.5.25"


def write_pair(directory, *, ip=IP, days=90, starts=-1):
    directory.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now + timedelta(days=starts))
            .not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(make_cert.build_san(ip)), False)
            .sign(key, hashes.SHA256()))
    (directory / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))


def startup(directory, monkeypatch, capsys):
    config = AppConfig(server=ServerConfig(cert_dir=str(directory)))
    config.asr.engine = config.mt.engine = "fake"
    monkeypatch.setattr(main, "load_config", lambda _: config)
    monkeypatch.setattr("sys.argv", ["server", "--config", "custom config.yaml"])
    info = {}

    async def serve(app, config, *, open_browser):
        with TestClient(app) as client:
            info.update(client.get("/api/teacher-info").json())

    monkeypatch.setattr(main, "_serve", serve)
    main.main()
    return capsys.readouterr().out, info


def test_stale_san_is_not_advertised_as_remote_ready(tmp_path, monkeypatch, capsys):
    write_pair(tmp_path, ip="192.168.1.35")
    output, info = startup(tmp_path, monkeypatch, capsys)
    assert "SAN不一致" in output
    assert "別端末可" not in output
    assert info["teacher_url"] == "http://127.0.0.1:8000/teacher"
    assert info["tls"]["certificate"]["status"] == "failed"
    assert '--config "custom config.yaml"' in output
    assert f"--advertise-ip {IP} --force" in output


def cli(directory, monkeypatch, *args):
    config = directory / "custom.yaml"
    config.write_text(f"server:\n  cert_dir: '{directory.as_posix()}'\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["make_cert", "--config", str(config),
                                    "--advertise-ip", IP, *args])
    return make_cert.main()


def test_reissue_keeps_pair_and_restore_recovers_exact_original(tmp_path, monkeypatch, capsys):
    write_pair(tmp_path, ip="192.168.1.35")
    original = [(tmp_path / name).read_bytes() for name in ("cert.pem", "key.pem")]
    assert cli(tmp_path, monkeypatch, "--force") == 0
    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1
    assert [(backups[0] / name).read_bytes() for name in ("cert.pem", "key.pem")] == original
    cert = x509.load_pem_x509_certificate((tmp_path / "cert.pem").read_bytes())
    assert IP in {str(ip) for ip in cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)}
    assert cli(tmp_path, monkeypatch, "--restore", str(backups[0])) == 0
    assert [(tmp_path / name).read_bytes() for name in ("cert.pem", "key.pem")] == original
    assert "SAN不一致" in capsys.readouterr().out


@pytest.mark.parametrize("problem,detail", [
    ("expired", "期限切れ"), ("future", "有効期間前"), ("mismatch", "鍵不整合"),
    ("missing", "証明書なし"), ("corrupt", "形式"), ("denied", "未確認"),
])
def test_bad_certificate_never_reports_remote_ready(tmp_path, monkeypatch, capsys, problem, detail):
    if problem == "expired":
        write_pair(tmp_path, days=-1, starts=-100)
    elif problem == "future":
        write_pair(tmp_path, starts=1)
    elif problem != "missing":
        write_pair(tmp_path)
    if problem == "mismatch":
        write_pair(tmp_path / "other")
        (tmp_path / "key.pem").write_bytes((tmp_path / "other/key.pem").read_bytes())
    if problem == "corrupt":
        (tmp_path / "cert.pem").write_bytes(b"broken")
    if problem == "denied":
        original_read = Path.read_bytes

        def read(path):
            if path == tmp_path / "cert.pem":
                raise PermissionError()
            return original_read(path)

        monkeypatch.setattr(Path, "read_bytes", read)
    output, info = startup(tmp_path, monkeypatch, capsys)
    assert detail in output
    assert "別端末可" not in output
    assert "--force" in output
    assert info["teacher_url"] == "http://127.0.0.1:8000/teacher"


def test_good_certificate_leaves_evidence_but_remote_use_is_unknown(tmp_path, monkeypatch, capsys):
    write_pair(tmp_path)
    output, info = startup(tmp_path, monkeypatch, capsys)
    assert "SHA256:" in output and "有効期間:" in output and "検証時刻:" in output
    assert "--force" not in output
    assert info["teacher_url"] == f"https://{IP}:8443/teacher"
    assert info["tls"]["remote_trust"]["status"] == "unknown"


def test_unselected_network_does_not_check_certificates(tmp_path, monkeypatch):
    from server.diagnostics import inspect_tls

    write_pair(tmp_path)
    report = inspect_tls(ServerConfig(cert_dir=str(tmp_path)), None)
    assert report["certificate"]["status"] == "unknown"
    assert report["key_pair"]["status"] == "unknown"


def test_tls_diagnostic_checks_live_https_and_keeps_remote_unknown(tmp_path):
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from server.diagnostics import inspect_tls
    from tests.net_guard import guard_network

    write_pair(tmp_path, ip="127.0.0.1")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/healthz"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok","ready":false}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "cert.pem", tmp_path / "key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with guard_network() as guard:
            report = inspect_tls(ServerConfig(cert_dir=str(tmp_path), https_port=server.server_port), "127.0.0.1")
        guard.assert_no_external_traffic()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert report["served_certificate"]["status"] == "ok"
    assert report["https_health"]["status"] == "ok"
    assert report["remote_trust"]["status"] == "unknown"


def test_second_file_install_failure_restores_both_originals(tmp_path, monkeypatch):
    import os

    write_pair(tmp_path, ip="192.168.1.35")
    original = [(tmp_path / name).read_bytes() for name in ("cert.pem", "key.pem")]
    replace = os.replace
    failed = False

    def fail_once(source, destination):
        nonlocal failed
        if destination == tmp_path / "key.pem" and not failed:
            failed = True
            raise PermissionError("injected key replacement failure")
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_once)
    assert cli(tmp_path, monkeypatch, "--force") == 1
    assert failed
    assert [(tmp_path / name).read_bytes() for name in ("cert.pem", "key.pem")] == original


@pytest.mark.parametrize("missing", ["cert.pem", "key.pem"])
def test_partial_pair_requires_force_and_restores_missing_file_state(tmp_path, monkeypatch, missing):
    write_pair(tmp_path)
    (tmp_path / missing).unlink()
    assert cli(tmp_path, monkeypatch) == 1
    assert not (tmp_path / missing).exists()
    assert cli(tmp_path, monkeypatch, "--force") == 0
    backup = next((tmp_path / "backups").iterdir())
    assert cli(tmp_path, monkeypatch, "--restore", str(backup)) == 0
    assert not (tmp_path / missing).exists()


def test_damaged_backup_is_rejected_without_changing_active_pair(tmp_path, monkeypatch):
    write_pair(tmp_path)
    assert cli(tmp_path, monkeypatch, "--force") == 0
    backup = next((tmp_path / "backups").iterdir())
    (backup / "key.pem").unlink()
    original = [(tmp_path / name).read_bytes() for name in ("cert.pem", "key.pem")]
    assert cli(tmp_path, monkeypatch, "--restore", str(backup)) == 1
    assert [(tmp_path / name).read_bytes() for name in ("cert.pem", "key.pem")] == original


def test_unselected_network_preserves_existing_pair(tmp_path, monkeypatch):
    write_pair(tmp_path)
    original = [(tmp_path / name).read_bytes() for name in ("cert.pem", "key.pem")]
    monkeypatch.setattr("server.network.list_addresses", lambda: [])
    assert cli(tmp_path, monkeypatch, "--force") == 1
    assert [(tmp_path / name).read_bytes() for name in ("cert.pem", "key.pem")] == original
    assert not (tmp_path / "backups").exists()


def test_existing_bad_certificate_is_reported_without_overwriting(tmp_path, monkeypatch, capsys):
    write_pair(tmp_path, ip="192.168.1.35")
    original = (tmp_path / "cert.pem").read_bytes()
    assert cli(tmp_path, monkeypatch) == 1
    assert (tmp_path / "cert.pem").read_bytes() == original
    assert "SAN不一致" in capsys.readouterr().out
