"""起動、先生情報、診断、証明書が同じ公開IPを使う（#38）。"""

import pytest
from starlette.testclient import TestClient

from server.main import create_app
from server.network import InterfaceAddress
from tests.conftest import JOIN_CODE, make_ws_test_config
from server.asr.fake_engine import FakeASREngine
from server.mt.fake_engine import FakeTranslationEngine


def make_app(config):
    return create_app(config, asr_engine=FakeASREngine(),
                      mt_engine=FakeTranslationEngine(["en", "zh"]), join_code=JOIN_CODE)


def test_configured_wifi_controls_urls_then_disappears(monkeypatch):
    addresses = [
        InterfaceAddress("Ethernet", "192.168.1.42", True, "physical"),
        InterfaceAddress("Wi-Fi", "10.53.64.130", True, "physical"),
    ]
    monkeypatch.setattr("server.network.list_addresses", lambda: addresses)
    config = make_ws_test_config()
    config.server.advertise_ip = "10.53.64.130"
    with TestClient(make_app(config)) as client:
        info = client.get("/api/teacher-info").json()
        assert info["join_url"] == "http://10.53.64.130:8000/?code=4831"
        assert "http://10.53.64.130:8000/healthz" in client.get("/connection-help").text
        addresses.pop()
        response = client.get("/api/teacher-info")
        assert response.status_code == 503
        assert "join_url" not in response.json()
        assert "再" in response.json()["detail"]
        assert client.get("/healthz").status_code == 200


def test_auto_selected_network_change_requires_restart(monkeypatch):
    addresses = [InterfaceAddress("Wi-Fi", "10.53.64.130", True, "physical")]
    monkeypatch.setattr("server.network.list_addresses", lambda: addresses)
    with TestClient(make_app(make_ws_test_config())) as client:
        assert client.get("/api/teacher-info").status_code == 200
        addresses[:] = [InterfaceAddress("Wi-Fi", "10.53.64.131", True, "physical")]
        response = client.get("/api/teacher-info")
        assert response.status_code == 503
        assert "join_url" not in response.json()


def test_certificate_cli_uses_same_configured_wifi(tmp_path, monkeypatch):
    import ipaddress
    from cryptography import x509
    from scripts import make_cert

    monkeypatch.setattr("server.network.list_addresses", lambda: [
        InterfaceAddress("Ethernet", "192.168.1.42", True, "physical"),
        InterfaceAddress("Wi-Fi", "10.53.64.130", True, "physical"),
    ])
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"server:\n  advertise_ip: 10.53.64.130\n  cert_dir: '{tmp_path.as_posix()}'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["make_cert", "--config", str(config_path)])
    assert make_cert.main() == 0
    cert = x509.load_pem_x509_certificate((tmp_path / "cert.pem").read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert ipaddress.IPv4Address("10.53.64.130") in san.get_values_for_type(x509.IPAddress)
    assert ipaddress.IPv4Address("192.168.1.42") not in san.get_values_for_type(x509.IPAddress)


def test_startup_api_and_diagnostics_publish_same_selected_ip(tmp_path, monkeypatch, capsys):
    from server import main, diagnostics

    config = make_ws_test_config()
    config.asr.engine = "fake"
    config.mt.engine = "fake"
    config.server.advertise_ip = "10.53.64.130"
    config.server.cert_dir = str(tmp_path)
    from scripts.make_cert import generate
    generate(tmp_path / "cert.pem", tmp_path / "key.pem", ip="10.53.64.130")
    monkeypatch.setattr("server.network.list_addresses", lambda: [
        InterfaceAddress("Ethernet", "192.168.1.42", True, "physical"),
        InterfaceAddress("Wi-Fi", "10.53.64.130", True, "physical"),
    ])
    monkeypatch.setattr(main, "load_config", lambda _: config)
    monkeypatch.setattr("sys.argv", ["server"])
    urls = {}

    async def serve(app, config, *, open_browser):
        with TestClient(app) as client:
            urls.update(client.get("/api/teacher-info").json())

    monkeypatch.setattr(main, "_serve", serve)
    main.main()
    startup = capsys.readouterr().out
    assert urls["join_url"] in startup
    assert urls["teacher_url"] == "https://10.53.64.130:8443/teacher"
    assert urls["teacher_url"] in startup
    monkeypatch.setattr(diagnostics, "inspect_windows", lambda _: {})
    monkeypatch.setattr(diagnostics, "inspect_tls", lambda _, ip: {
        "selected": ip, "certificate": {"status": "unknown"}, "key_pair": {"status": "unknown"},
    })
    monkeypatch.setattr(diagnostics, "probe_http", lambda host, port, path: {"status": "ok", "host": host})
    report = diagnostics.diagnose(config)
    assert report["network"]["selected_ip"] == "10.53.64.130"
    assert report["tls"]["selected"] == "10.53.64.130"
    assert report["probes"]["selected_ip_http"]["host"] == "10.53.64.130"


def test_invalid_selection_is_not_probed_or_advertised(monkeypatch):
    from server import diagnostics

    config = make_ws_test_config()
    config.server.advertise_ip = "10.53.64.130"
    hosts = []
    monkeypatch.setattr(diagnostics, "inspect_windows", lambda _: {})
    monkeypatch.setattr(diagnostics, "inspect_tls", lambda _, ip: {
        "selected": ip, "certificate": {"status": "unknown"}, "key_pair": {"status": "unknown"},
    })

    def probe(host, port, path):
        hosts.append(host)
        return {"status": "ok"}

    monkeypatch.setattr(diagnostics, "probe_http", probe)
    report = diagnostics.diagnose(config)
    assert report["network"]["selected_ip"] is None
    assert report["probes"]["selected_ip_http"]["status"] == "unknown"
    assert report["tls"]["selected"] is None
    assert hosts == ["127.0.0.1", "127.0.0.1"]
    with TestClient(make_app(config)) as client:
        assert client.get("/api/teacher-info").status_code == 503
