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


def write_pair(directory, *, ip=IP, days=90, starts=-1, names=None):
    directory.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now + timedelta(days=starts))
            .not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(make_cert.build_san(ip) if names is None else names), False)
            .sign(key, hashes.SHA256()))
    (directory / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))


def startup(directory, monkeypatch, capsys):
    config = AppConfig(server=ServerConfig(cert_dir=str(directory)))
    config.asr.engine = config.mt.engine = "fake"
    monkeypatch.setattr(main, "load_config", lambda *_, **__: config)
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


def cli(directory, monkeypatch, *args, ip=IP):
    config = directory / "custom.yaml"
    config.write_text(f"server:\n  cert_dir: '{directory.as_posix()}'\n", encoding="utf-8")
    selected = ["--advertise-ip", ip] if ip else []
    monkeypatch.setattr("sys.argv", ["make_cert", "--config", str(config), *selected, *args])
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
    assert cli(tmp_path, monkeypatch) == make_cert.EXIT_UNCHANGED_NOT_READY
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
    # setup.ps1 は2を警告として続行する。起動時も同じ検証でlocalhost先生URLに縮退する。
    assert cli(tmp_path, monkeypatch) == make_cert.EXIT_UNCHANGED_NOT_READY
    assert (tmp_path / "cert.pem").read_bytes() == original
    assert "SAN不一致" in capsys.readouterr().out


def test_existing_pair_with_unresolved_network_is_unchanged_not_failed(tmp_path, monkeypatch, capsys):
    write_pair(tmp_path)
    original = [(tmp_path / name).read_bytes() for name in ("cert.pem", "key.pem")]
    monkeypatch.setattr("server.network.list_addresses", lambda: [])
    # setup.ps1 と同じく --advertise-ip なし（自動選択）
    assert cli(tmp_path, monkeypatch, ip=None) == make_cert.EXIT_UNCHANGED_NOT_READY
    assert [(tmp_path / name).read_bytes() for name in ("cert.pem", "key.pem")] == original
    assert not (tmp_path / "backups").exists()
    assert "未確認" in capsys.readouterr().out


def test_unchanged_code_differs_from_argparse_usage_error():
    # argparse の引数エラーは2。setup.ps1 がそれを警告として続行しないよう別の値にする。
    assert make_cert.EXIT_UNCHANGED_NOT_READY not in (0, 1, 2)


def test_explicit_ip_not_on_this_pc_fails_even_with_existing_pair(tmp_path, monkeypatch):
    write_pair(tmp_path)
    monkeypatch.setattr("server.network.list_addresses", lambda: [])
    assert cli(tmp_path, monkeypatch) == 1  # cli は --advertise-ip を明示する


def serve_once(config, monkeypatch):
    """本番の _serve をループバック・空きポートで1回起動し、提供中のTLSを診断して終了する。

    起動ごとにアプリを作り直す（停止→再起動の模擬）。待受とTLSは実物、推論だけフェイク。
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    import uvicorn
    from server import event_loop
    from server.asr.fake_engine import FakeASREngine
    from server.diagnostics import inspect_tls
    from server.mt.fake_engine import FakeTranslationEngine
    from tests.conftest import JOIN_CODE
    from tests.net_guard import guard_network

    servers = []
    original_config, original_server = uvicorn.Config, uvicorn.Server

    def loopback_config(*args, **kwargs):
        kwargs.update(host="127.0.0.1", log_config=None, access_log=False)
        return original_config(*args, **kwargs)

    def observe_server(*args, **kwargs):
        servers.append(original_server(*args, **kwargs))
        return servers[-1]

    monkeypatch.setattr(uvicorn, "Config", loopback_config)
    monkeypatch.setattr(uvicorn, "Server", observe_server)
    app = main.create_app(config, asr_engine=FakeASREngine(),
                          mt_engine=FakeTranslationEngine(["en", "zh"]), join_code=JOIN_CODE)

    async def scenario():
        serving = asyncio.create_task(main._serve(app, config, open_browser=False))
        try:
            async with asyncio.timeout(10):
                while not servers or not all(server.started for server in servers):
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)  # 2つ目の待受の生成を待つ（HTTPSなしなら1つのまま）
            assert all(server.started for server in servers)
            ports = [server.servers[0].sockets[0].getsockname()[1] for server in servers]
            if len(ports) == 1:
                return ports, None
            probe = ServerConfig(cert_dir=config.server.cert_dir, https_port=ports[1])
            with guard_network() as guard:
                report = await asyncio.to_thread(inspect_tls, probe, "127.0.0.1")
            guard.assert_no_external_traffic()
            return ports, report
        finally:
            for server in servers:
                server.should_exit = True
            await asyncio.wait_for(serving, timeout=10)

    # 本番のイベントループ。ワーカースレッドなので本番のシグナル設定は pytest を置き換えない。
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: event_loop.run(scenario())).result(timeout=30)


@pytest.mark.timeout(60)
def test_reissue_and_restore_take_effect_on_restarted_production_listener(tmp_path, monkeypatch):
    """OPS-08: 旧SAN→HTTPSなし、再発行→再起動で提供中証明書がファイルと一致、復元→元の一組。"""
    import ipaddress
    from tests.conftest import make_ws_test_config

    stale = [x509.IPAddress(ipaddress.ip_address("192.168.1.35"))]
    write_pair(tmp_path, names=stale)
    paths = (tmp_path / "cert.pem", tmp_path / "key.pem")
    original = [path.read_bytes() for path in paths]
    config = make_ws_test_config()
    config.server.cert_dir = str(tmp_path)
    config.server.advertise_ip = "127.0.0.1"
    config.server.http_port = config.server.https_port = 0

    ports, report = serve_once(config, monkeypatch)
    assert len(ports) == 1 and report is None  # 旧SANではHTTPSを待ち受けない

    make_cert.replace_pair(*paths, ip="127.0.0.1", restore=None)
    backup = next((tmp_path / "backups").iterdir())
    ports, report = serve_once(config, monkeypatch)
    assert len(ports) == 2

    def sha256(pem):
        return x509.load_pem_x509_certificate(pem).fingerprint(hashes.SHA256()).hex()

    assert sha256(paths[0].read_bytes()) != sha256(original[0])
    assert {name: report[name]["status"] for name in report} == {
        "certificate": "ok", "key_pair": "ok", "served_certificate": "ok",
        "https_health": "ok", "remote_trust": "unknown"}
    assert report["served_certificate"]["sha256"] == sha256(paths[0].read_bytes())

    make_cert.replace_pair(*paths, ip="127.0.0.1", restore=backup)
    assert [path.read_bytes() for path in paths] == original
    ports, report = serve_once(config, monkeypatch)
    assert len(ports) == 1 and report is None  # 復元成功はTLS正常ではない
