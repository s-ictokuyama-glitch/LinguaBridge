"""運用パッケージ（イシュー#16）のサーバー側挙動テスト。

- /ready がモデルロード完了まで 503、完了で 200（E-13）。/healthz は liveness で常に200（#25 W-2）
- /api/teacher-info は HTTPS からは許可、平文HTTPの非ループバックは 403
- モデル欠損時に build_*_engine が復旧手順つきで失敗する
"""

from __future__ import annotations

import threading
import time

import pytest
from starlette.testclient import TestClient

from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, AsrConfig, ModelsConfig, MtConfig
from server.main import build_asr_engine, cert_days_remaining, create_app
from server.mt.fake_engine import FakeTranslationEngine
from server.network import InterfaceAddress
from tests.conftest import JOIN_CODE, make_ws_test_config


def make_app(*, asr_engine=None):
    return create_app(
        make_ws_test_config(),
        asr_engine=asr_engine or FakeASREngine(),
        mt_engine=FakeTranslationEngine(["en", "zh"]),
        join_code=JOIN_CODE,
    )


class TestHealthzReadiness:
    def test_503_until_model_loaded_then_200(self):
        gate = threading.Event()  # warmup を止めてロード中を再現
        app = make_app(asr_engine=FakeASREngine(warmup_gate=gate))
        with TestClient(app) as client:
            assert client.get("/ready").status_code == 503  # ロード中
            gate.set()
            for _ in range(200):
                if client.get("/ready").status_code == 200:
                    break
                time.sleep(0.02)
            assert client.get("/ready").status_code == 200

    def test_liveness_is_200_even_while_models_load(self):
        """liveness と readiness の分離（#25 W-2）。ロード中でもプロセスは生きている。"""
        gate = threading.Event()
        app = make_app(asr_engine=FakeASREngine(warmup_gate=gate))
        try:
            with TestClient(app) as client:
                assert client.get("/ready").status_code == 503
                res = client.get("/healthz")
                assert res.status_code == 200
                assert res.json() == {"status": "ok", "ready": False}
        finally:
            gate.set()  # 止めた warmup スレッドを解放


class TestTeacherInfoAccess:
    def test_https_scheme_allowed_from_non_loopback(self):
        # 先生ページは別端末のHTTPSで開くので、非ループバックでも https なら許可
        app = make_app()
        with TestClient(
            app, base_url="https://192.168.1.50", client=("192.168.1.50", 55000)
        ) as client:
            res = client.get("/api/teacher-info")
            assert res.status_code == 200
            assert res.json()["code"] == JOIN_CODE

    def test_plain_http_non_loopback_forbidden(self):
        # 平文HTTP（生徒用）の非ループバックからは参加コードを渡さない
        app = make_app()
        with TestClient(
            app, base_url="http://192.168.1.50", client=("192.168.1.50", 55000)
        ) as client:
            assert client.get("/api/teacher-info").status_code == 403


class TestModelValidation:
    def test_missing_model_reports_recovery_steps(self, tmp_path):
        config = AppConfig(
            models=ModelsConfig(dir=str(tmp_path)),
            asr=AsrConfig(engine="faster-whisper", model="faster-whisper-small"),
            mt=MtConfig(engine="fake"),
        )
        with pytest.raises(FileNotFoundError, match="download_models"):
            build_asr_engine(config)


def _write_cert(path, days_valid: int) -> None:
    """指定日数だけ有効な自己署名証明書を書き出す（E-15の残存期間チェック検証用）。"""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # not_valid_before は常に after より前（days_valid が負=期限切れでも成立させる）
        .not_valid_before(now - datetime.timedelta(days=400))
        .not_valid_after(now + datetime.timedelta(days=days_valid))
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


class TestCertExpiry:
    def test_remaining_days_for_fresh_cert(self, tmp_path):
        cert = tmp_path / "cert.pem"
        _write_cert(cert, days_valid=100)
        days = cert_days_remaining(cert)
        assert days is not None and 98 <= days <= 100

    def test_expired_cert_reports_negative(self, tmp_path):
        cert = tmp_path / "cert.pem"
        _write_cert(cert, days_valid=-5)
        days = cert_days_remaining(cert)
        assert days is not None and days < 0

    def test_missing_or_bogus_cert_returns_none(self, tmp_path):
        assert cert_days_remaining(tmp_path / "nope.pem") is None
        bogus = tmp_path / "bogus.pem"
        bogus.write_text("not a certificate")
        assert cert_days_remaining(bogus) is None


class TestConnectionDiagnostics:
    @pytest.mark.timeout(90)
    def test_windows_launcher_preserves_os_settings(self):
        import json
        import os
        import subprocess
        from pathlib import Path

        if os.name != "nt":
            pytest.skip("Windows OS設定の比較")
        root = Path(__file__).resolve().parents[2]
        snapshot = r"""
        $ErrorActionPreference = 'Stop'
        try {
            @{
                profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore | Sort-Object Name | Select-Object Name, Enabled, DefaultInboundAction, AllowInboundRules, AllowLocalFirewallRules)
                rules = @(Get-NetFirewallRule -PolicyStore ActiveStore | Sort-Object Name | Select-Object Name, Enabled, Direction, Action, Profile)
                addresses = @(Get-NetIPAddress -AddressFamily IPv4 | Sort-Object InterfaceIndex, IPAddress | Select-Object InterfaceIndex, IPAddress, PrefixLength)
                dns = @(Get-DnsClientServerAddress | Sort-Object InterfaceIndex, AddressFamily | Select-Object InterfaceIndex, AddressFamily, ServerAddresses)
                policies = @(Get-ExecutionPolicy -List | Select-Object Scope, ExecutionPolicy)
                roots = @(Get-ChildItem Cert:\LocalMachine\Root, Cert:\CurrentUser\Root | Sort-Object Thumbprint | Select-Object Thumbprint)
            } | ConvertTo-Json -Depth 5 -Compress
        } catch { exit 3 }
        """

        def settings():
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", snapshot],
                capture_output=True, text=True, timeout=25,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode:
                pytest.skip("OS設定を取得する権限が無いため比較は未確認")
            return json.loads(result.stdout)

        before = settings()
        paths = [root / "config.yaml", root / ".venv" / ".setup-complete"]
        paths.extend((root / "certs").glob("*.pem"))
        files_before = {path: path.read_bytes() for path in paths if path.is_file()}
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(root / "scripts" / "run.ps1"), "-Diagnose"],
            cwd=root, capture_output=True, timeout=35, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert result.returncode == 0
        assert settings() == before
        assert {path: path.read_bytes() for path in files_before} == files_before

    def test_help_uses_configured_ports_and_separates_unconfirmed_stages(self, monkeypatch):
        config = make_ws_test_config()
        config.server.http_port = 18081
        config.server.https_port = 18444
        monkeypatch.setattr("server.main.get_lan_ip", lambda: "192.168.5.25")
        app = create_app(config, asr_engine=FakeASREngine(),
                         mt_engine=FakeTranslationEngine(["en", "zh"]), join_code=JOIN_CODE)
        with TestClient(app) as client:
            response = client.get("/connection-help")
            assert response.status_code == 200
            assert "http://192.168.5.25:18081/healthz" in response.text
            assert "https://192.168.5.25:18444/healthz" in response.text
            assert "http://127.0.0.1:18081/healthz" in response.text
            assert "start.bat --diagnose" in response.text
            assert "AP分離と断定" in response.text
            assert "モデル" in response.text
            assert "WS参加: 未確認" in response.text
            assert JOIN_CODE not in response.text
            assert "?code=" not in response.text
            assert 'href="/connection-help"' in client.get("/").text
            assert 'href="/connection-help"' in client.get("/teacher").text

    def test_unavailable_observations_are_unknown_and_report_has_no_secrets(
        self, tmp_path, monkeypatch, capsys
    ):
        import json
        import socket
        import subprocess
        from http.client import HTTPConnection

        from server.diagnostics import main

        config = tmp_path / "config.yaml"
        config.write_text(
            "server:\n  http_port: 18081\n  https_port: 18444\n"
            f"  cert_dir: '{tmp_path.as_posix()}'\n",
            encoding="utf-8",
        )

        def denied(*args, **kwargs):
            raise PermissionError("sensitive detail must not be included")

        monkeypatch.setattr("server.network.list_addresses", lambda: [])
        monkeypatch.setattr(socket, "getaddrinfo", denied)
        monkeypatch.setattr(subprocess, "run", denied)
        monkeypatch.setattr(HTTPConnection, "connect", denied)
        before = config.read_bytes()
        assert main(["--config", str(config), "--json"]) == 0
        output = capsys.readouterr().out
        report = json.loads(output)
        assert report["ports"] == {"http": 18081, "https": 18444}
        assert report["network"]["status"] == "unknown"
        assert report["network"]["usable_for_remote"] is False
        assert "別端末用の接続先として案内できません" in report["next_steps"][0]
        assert report["listeners"]["status"] == "unknown"
        assert report["firewall"]["status"] == "unknown"
        assert report["probes"]["loopback_http"]["status"] == "unknown"
        assert set(report["remote"]) == {"http", "tls", "page", "ws_join"}
        assert all(check["status"] == "unknown" for check in report["remote"].values())
        assert report["runtime"]["python"]
        assert report["record"]["next_stage"] == ""
        assert "sensitive detail" not in output
        assert JOIN_CODE not in output
        assert "PRIVATE KEY" not in output
        assert config.read_bytes() == before

    def test_live_http_is_distinct_from_models_and_remote_checks(self, tmp_path, monkeypatch):
        import json
        import socket
        import subprocess
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from server.diagnostics import diagnose

        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(503 if self.path == "/ready" else 200)
                self.end_headers()
                self.wfile.write({
                    "/healthz": b'{"status":"ok","ready":false}',
                    "/ready": b'{"status":"loading"}',
                    "/": b"<!doctype html><title>LinguaBridge</title>",
                }[self.path])

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        config = make_ws_test_config()
        config.server.http_port = server.server_port
        config.server.cert_dir = str(tmp_path)
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", server.server_port))
        ])

        def windows_read(args, **kwargs):
            # OS境界: 読み取り専用スクリプトだけを受け付ける。
            assert args[args.index("-File") + 1].endswith("diagnose_windows.ps1")
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "listeners": {"status": "observed", "detail": "取得済み", "items": [
                    {"LocalAddress": "127.0.0.1", "LocalPort": server.server_port,
                     "OwningProcess": 123, "ProcessName": "python"}
                ]},
                "firewall": {"status": "unknown", "detail": "権限不足"},
            }), "")

        monkeypatch.setattr(subprocess, "run", windows_read)
        monkeypatch.setattr("platform.system", lambda: "Windows")
        try:
            report = diagnose(config)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        assert report["probes"]["loopback_http"]["status"] == "ok"
        assert report["probes"]["selected_ip_http"]["status"] == "ok"
        assert report["probes"]["models"]["status"] == "failed"
        assert report["probes"]["models"]["http_status"] == 503
        assert report["probes"]["page"]["status"] == "ok"
        assert report["listeners"]["items"][0]["ProcessName"] == "python"
        assert report["firewall"]["status"] == "unknown"
        assert all(item["status"] == "unknown" for item in report["remote"].values())
        assert set(paths) == {"/healthz", "/ready", "/"}

    def test_slow_firewall_does_not_discard_listener_results(self, tmp_path, monkeypatch):
        import json
        import subprocess
        from http.client import HTTPConnection

        from server.diagnostics import diagnose

        def slow(args, **kwargs):
            output = json.dumps({"listeners": {
                "status": "observed", "detail": "取得済み", "items": [{"OwningProcess": 456}]
            }}).encode("utf-8")
            raise subprocess.TimeoutExpired(args, 20, output=output)

        def denied(*args, **kwargs):
            raise PermissionError()

        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.setattr(subprocess, "run", slow)
        monkeypatch.setattr(HTTPConnection, "connect", denied)
        config = make_ws_test_config()
        config.server.cert_dir = str(tmp_path)
        report = diagnose(config)
        assert report["listeners"]["items"] == [{"OwningProcess": 456}]
        assert report["firewall"]["status"] == "unknown"

    def test_tls_checks_ip_and_key_without_changing_or_exporting_files(self, tmp_path, monkeypatch):
        import json
        import socket
        from http.client import HTTPConnection

        from scripts.make_cert import generate
        from server.diagnostics import diagnose

        monkeypatch.setattr("scripts.make_cert.get_lan_ip", lambda: "192.168.5.25")
        cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        generate(cert, key)
        config = make_ws_test_config()
        config.server.cert_dir = str(tmp_path)
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.5.25", 0))
        ])

        def denied(*args, **kwargs):
            raise PermissionError()

        monkeypatch.setattr(HTTPConnection, "connect", denied)
        monkeypatch.setattr("subprocess.run", denied)
        original = (cert.read_bytes(), key.read_bytes())
        report = diagnose(config)
        assert report["tls"]["certificate"]["status"] == "ok"
        assert report["tls"]["key_pair"]["status"] == "ok"
        assert report["tls"]["remote_trust"]["status"] == "unknown"
        assert (cert.read_bytes(), key.read_bytes()) == original
        assert "PRIVATE KEY" not in json.dumps(report)
        monkeypatch.setattr("server.network.list_addresses", lambda: [
            InterfaceAddress("Wi-Fi", "192.168.5.26", True, "physical")
        ])
        assert diagnose(config)["tls"]["certificate"]["status"] == "failed"
        generate(tmp_path / "other.pem", key)
        assert diagnose(config)["tls"]["key_pair"]["status"] == "failed"
