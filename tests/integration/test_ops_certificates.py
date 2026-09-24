"""OPS-09/OPS-11 の証明書フォールバック・生成物・起動案内を検証する。"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from cryptography import x509

import scripts.make_cert as make_cert
import server.main as server_main
from server.config import AppConfig, ServerConfig
from tests.integration.test_certificate_workflow import write_pair


def test_startup_without_certificate_prints_local_http_teacher_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cert_dir = tmp_path / "empty-certs"
    cert_dir.mkdir()
    config = AppConfig(
        server=ServerConfig(http_port=18009, cert_dir=str(cert_dir))
    )
    app = SimpleNamespace(state=SimpleNamespace(session=SimpleNamespace(join_code="1234")))
    serve_calls: list[tuple[object, AppConfig, bool]] = []

    async def fake_serve(
        candidate_app: object, candidate_config: AppConfig, *, open_browser: bool
    ) -> None:
        serve_calls.append((candidate_app, candidate_config, open_browser))

    monkeypatch.setattr(sys, "argv", ["server/main.py", "--config", "isolated.yaml"])
    monkeypatch.setattr(server_main, "load_config", lambda *_, **__: config)
    monkeypatch.setattr(server_main, "create_app", lambda _: app)
    monkeypatch.setattr(server_main, "get_lan_ip", lambda: "192.168.50.23")
    monkeypatch.setattr(server_main, "_serve", fake_serve)

    server_main.main()

    output = capsys.readouterr().out
    assert config.server.tls_ready() is False
    assert "http://127.0.0.1:18009/teacher" in output
    assert "証明書なし" in output
    teacher_lines = [line.strip() for line in output.splitlines() if "先生ページ" in line]
    assert teacher_lines == [
        "先生ページ : http://127.0.0.1:18009/teacher（このPCで開く。別端末HTTPSは証明書の修復後に再確認）"
    ]
    assert serve_calls == [(app, config, False)]


def test_serve_without_certificate_is_http_only_and_opens_local_teacher_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cert_dir = tmp_path / "empty-certs"
    cert_dir.mkdir()
    config = AppConfig(
        server=ServerConfig(http_port=18009, https_port=18443, cert_dir=str(cert_dir))
    )
    pipeline = object()
    app = SimpleNamespace(state=SimpleNamespace(pipeline=pipeline))
    uvicorn_configs: list[dict[str, object]] = []
    served_configs: list[object] = []
    opened_pages: list[tuple[object, str]] = []

    def fake_uvicorn_config(candidate_app: object, **kwargs: object) -> object:
        configured = SimpleNamespace(app=candidate_app, options=kwargs)
        uvicorn_configs.append({"app": candidate_app, **kwargs})
        return configured

    class FakeServer:
        def __init__(self, configured: object) -> None:
            self.configured = configured
            self.should_exit = False

        async def serve(self) -> None:
            served_configs.append(self.configured)
            await asyncio.sleep(0)

    async def fake_open_teacher_page(candidate_pipeline: object, url: str) -> None:
        opened_pages.append((candidate_pipeline, url))

    monkeypatch.setattr(uvicorn, "Config", fake_uvicorn_config)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(signal, "signal", lambda *_: None)
    monkeypatch.setattr(
        server_main, "_open_teacher_page_when_ready", fake_open_teacher_page
    )

    asyncio.run(server_main._serve(app, config, open_browser=True))

    assert config.server.tls_ready() is False
    assert uvicorn_configs == [
        {
            "app": app,
            "host": "0.0.0.0",
            "port": 18009,
            "log_level": "info",
        }
    ]
    assert len(served_configs) == 1
    assert opened_pages == [(pipeline, "http://127.0.0.1:18009/teacher")]


def test_generate_writes_required_sans_to_isolated_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lan_ip = "192.168.50.23"
    cert_path = tmp_path / "isolated-certs" / "cert.pem"
    key_path = tmp_path / "isolated-certs" / "key.pem"
    monkeypatch.setattr(make_cert, "get_lan_ip", lambda: lan_ip)

    make_cert.generate(cert_path, key_path)

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns_names = set(san.get_values_for_type(x509.DNSName))
    ip_addresses = {str(value) for value in san.get_values_for_type(x509.IPAddress)}
    assert "localhost" in dns_names
    assert {"127.0.0.1", lan_ip} <= ip_addresses
    assert key_path.is_file()


def _run_startup_banner(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    days_remaining: int | None,
    use_real_expiry_check: bool = False,
) -> str:
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(b"not a certificate")
    key_path.write_bytes(b"not a private key")
    config = AppConfig(server=ServerConfig(cert_dir=str(tmp_path)))
    app = SimpleNamespace(state=SimpleNamespace(session=SimpleNamespace(join_code="1234")))
    serve_calls: list[tuple[object, AppConfig, bool]] = []
    banners_before_serve: list[str] = []

    async def fake_serve(
        candidate_app: object, candidate_config: AppConfig, *, open_browser: bool
    ) -> None:
        banners_before_serve.append(capsys.readouterr().out)
        serve_calls.append((candidate_app, candidate_config, open_browser))

    monkeypatch.setattr(sys, "argv", ["server/main.py", "--config", "isolated.yaml"])
    monkeypatch.setattr(server_main, "load_config", lambda *_, **__: config)
    monkeypatch.setattr(server_main, "create_app", lambda _: app)
    monkeypatch.setattr(server_main, "get_lan_ip", lambda: "192.168.50.23")
    monkeypatch.setattr(server_main, "_serve", fake_serve)
    if days_remaining is not None:
        # 形式不正のファイルと内部関数のモックではなく、実際の証明書で期限を検証する。
        write_pair(tmp_path, ip="192.168.50.23", days=days_remaining + 0.5, starts=-100)

    server_main.main()

    assert serve_calls == [(app, config, False)]
    assert len(banners_before_serve) == 1
    assert capsys.readouterr().out == ""
    return banners_before_serve[0]


@pytest.mark.parametrize(
    ("days_remaining", "expected_state"),
    [(29, "残り29日"), (-1, "期限切れ")],
    ids=["valid-but-expiring", "expired"],
)
def test_startup_warns_and_shows_force_regeneration_command_below_30_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    days_remaining: int,
    expected_state: str,
) -> None:
    output = _run_startup_banner(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
        days_remaining=days_remaining,
    )

    assert "証明書の有効期限が近い/切れています" in output
    assert expected_state in output
    assert r"python scripts\make_cert.py --config" in output
    assert "--advertise-ip 192.168.50.23 --force" in output


def test_startup_does_not_warn_at_30_day_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = _run_startup_banner(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
        days_remaining=30,
    )

    assert "証明書の有効期限が近い/切れています" not in output
    assert "--force" not in output


def test_startup_handles_corrupt_certificate_without_false_expiry_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = _run_startup_banner(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
        days_remaining=None,
        use_real_expiry_check=True,
    )

    assert "証明書の有効期限が近い/切れています" not in output
    assert "形式" in output
    assert "--advertise-ip 192.168.50.23 --force" in output
