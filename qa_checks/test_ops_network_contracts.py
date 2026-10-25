"""OPS-08 / OPS-10 の、実端末を必要としないサーバー側境界を検証する。"""

from __future__ import annotations

import asyncio
from pathlib import Path
import signal
import sys
from types import SimpleNamespace

import pytest
from cryptography import x509
from starlette.testclient import TestClient
import uvicorn

import scripts.make_cert as make_cert
from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, ServerConfig
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
import server.main as server_main
from tests.conftest import JOIN_CODE, make_ws_test_config


def test_teacher_info_recomputes_join_url_from_current_ip_port_and_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IP は固定キャッシュせず、現在値・設定ポート・4桁コードを毎回組み立てる。"""

    config = make_ws_test_config()
    config.server.http_port = 18080
    app = create_app(
        config,
        asr_engine=FakeASREngine(),
        mt_engine=FakeTranslationEngine(["en", "zh"]),
        join_code=JOIN_CODE,
    )
    addresses = iter(("192.168.10.23", "192.168.77.41"))
    monkeypatch.setattr(server_main, "get_lan_ip", lambda: next(addresses))

    with TestClient(app, base_url="http://127.0.0.1") as client:
        first = client.get("/api/teacher-info").json()
        second = client.get("/api/teacher-info").json()

    assert first["code"] == second["code"] == JOIN_CODE
    assert len(first["code"]) == 4 and first["code"].isdigit()
    assert first["join_url"] == f"http://192.168.10.23:18080/?code={JOIN_CODE}"
    assert second["join_url"] == f"http://192.168.77.41:18080/?code={JOIN_CODE}"


def test_tls_ready_serves_http_and_https_and_opens_secure_teacher_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """証明書が揃うと、生徒HTTPと先生HTTPSを同時に待受ける。"""

    cert_dir = tmp_path / "certs"
    cert_dir.mkdir()
    (cert_dir / "cert.pem").write_text("isolated test certificate", encoding="ascii")
    (cert_dir / "key.pem").write_text("isolated test key", encoding="ascii")
    config = make_ws_test_config()
    config.server = ServerConfig(http_port=18008, https_port=18448, cert_dir=str(cert_dir))
    pipeline = object()
    app = SimpleNamespace(state=SimpleNamespace(pipeline=pipeline))
    configured: list[dict[str, object]] = []
    served: list[object] = []
    opened: list[tuple[object, str]] = []

    def fake_config(candidate_app: object, **kwargs: object) -> object:
        item = {"app": candidate_app, **kwargs}
        configured.append(item)
        return SimpleNamespace(**item)

    class FakeServer:
        def __init__(self, candidate_config: object) -> None:
            self.config = candidate_config
            self.should_exit = False
            self.install_signal_handlers = lambda: None

        async def serve(self) -> None:
            served.append(self.config)
            await asyncio.sleep(0)

    async def fake_open(candidate_pipeline: object, url: str) -> None:
        opened.append((candidate_pipeline, url))

    monkeypatch.setattr(uvicorn, "Config", fake_config)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    monkeypatch.setattr(server_main, "_open_teacher_page_when_ready", fake_open)

    asyncio.run(server_main._serve(app, config, open_browser=True))

    assert config.server.tls_ready() is True
    assert configured == [
        {
            "app": app,
            "host": "0.0.0.0",
            "port": 18008,
            "log_level": "info",
        },
        {
            "app": app,
            "host": "0.0.0.0",
            "port": 18448,
            "log_level": "warning",
            "lifespan": "off",
            "ssl_certfile": str(cert_dir / "cert.pem"),
            "ssl_keyfile": str(cert_dir / "key.pem"),
        },
    ]
    assert len(served) == 2
    assert opened == [(pipeline, "https://127.0.0.1:18448/teacher")]


def test_ops08_does_not_advertise_https_lan_url_when_certificate_san_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A certificate for an old DHCP address cannot secure the current LAN URL."""

    issued_for_ip = "192.0.2.10"
    current_ip = "192.0.2.11"
    cert_dir = tmp_path / "certs"
    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"
    monkeypatch.setattr(make_cert, "get_lan_ip", lambda: issued_for_ip)
    make_cert.generate(cert_path, key_path)

    certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    san_ips = {str(value) for value in san.get_values_for_type(x509.IPAddress)}
    assert issued_for_ip in san_ips
    assert current_ip not in san_ips

    config = AppConfig(
        server=ServerConfig(
            http_port=18008,
            https_port=18448,
            cert_dir=str(cert_dir),
        )
    )
    app = SimpleNamespace(
        state=SimpleNamespace(lesson=SimpleNamespace(join_code=JOIN_CODE))
    )

    async def fake_serve(
        _candidate_app: object,
        _candidate_config: AppConfig,
        *,
        open_browser: bool,  # noqa: ARG001
    ) -> None:
        return None

    monkeypatch.setattr(sys, "argv", ["server/main.py", "--config", "isolated.yaml"])
    monkeypatch.setattr(server_main, "load_config", lambda _: config)
    monkeypatch.setattr(server_main, "create_app", lambda _: app)
    monkeypatch.setattr(server_main, "get_lan_ip", lambda: current_ip)
    monkeypatch.setattr(server_main, "_serve", fake_serve)

    server_main.main()

    output = capsys.readouterr().out
    stale_url = f"https://{current_ip}:{config.server.https_port}/teacher"
    assert stale_url not in output, (
        "OPS-08 violation: the advertised HTTPS teacher URL uses the current "
        "LAN IP even though that IP is absent from the certificate SAN"
    )
