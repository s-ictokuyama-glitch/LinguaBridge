"""#45: サーバー・診断・証明書スクリプトを上書き設定とデータルートつきで起動できる。"""

from __future__ import annotations

import json
import socket
import subprocess
from http.client import HTTPConnection

from starlette.testclient import TestClient

from scripts import make_cert
from server import diagnostics, main
from server.network import InterfaceAddress
from tests.integration.test_certificate_workflow import IP, write_pair


def layered_files(tmp_path):
    base = tmp_path / "app" / "config.yaml"
    base.parent.mkdir()
    base.write_text("asr: { engine: fake }\nmt: { engine: fake }\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    override = data / "config.yaml"
    override.write_text("server: { http_port: 18123 }\n", encoding="utf-8")
    return base, override, data


def use_network(monkeypatch):
    monkeypatch.setattr("server.network.list_addresses", lambda: [
        InterfaceAddress("Wi-Fi", IP, True, "physical"),
    ])


def test_server_starts_with_override_and_data_root(tmp_path, monkeypatch, capsys):
    base, override, data = layered_files(tmp_path)
    write_pair(data / "certs")
    use_network(monkeypatch)
    monkeypatch.setattr("sys.argv", [
        "server", "--config", str(base), "--config-override", str(override),
        "--data-root", str(data),
    ])
    served = {}

    async def serve(app, config, *, open_browser):
        served["config"] = config
        with TestClient(app) as client:
            served.update(client.get("/api/teacher-info").json())

    monkeypatch.setattr(main, "_serve", serve)
    main.main()
    output = capsys.readouterr().out

    assert served["config"].server.http_port == 18123
    assert served["config"].recording.resolved_out_dir == data.resolve() / "sessions"
    # data 配下の証明書が採用される
    assert served["teacher_url"] == f"https://{IP}:8443/teacher"
    assert f"http://{IP}:18123/?code=" in output
    layer_args = f'--config-override "{override}" --data-root "{data}"'
    assert f'server.diagnostics --config "{base}" {layer_args}' in output


def test_server_without_layer_arguments_prints_the_same_hints(tmp_path, monkeypatch, capsys):
    base, _, _ = layered_files(tmp_path)
    use_network(monkeypatch)
    monkeypatch.setattr("sys.argv", ["server", "--config", str(base)])

    async def serve(app, config, *, open_browser):
        pass

    monkeypatch.setattr(main, "_serve", serve)
    main.main()
    output = capsys.readouterr().out

    assert f'server.diagnostics --config "{base}" --advertise-ip {IP} --json' in output
    assert "--config-override" not in output and "--data-root" not in output


def deny_os_observations(monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(subprocess, "run", denied)
    monkeypatch.setattr(HTTPConnection, "connect", denied)


def test_diagnostics_reads_override_and_certificates_under_data_root(tmp_path, monkeypatch, capsys):
    base, override, data = layered_files(tmp_path)
    write_pair(data / "certs", ip="192.168.1.35")  # 採用IPと SAN が合わない
    use_network(monkeypatch)
    deny_os_observations(monkeypatch)

    assert diagnostics.main([
        "--config", str(base), "--config-override", str(override),
        "--data-root", str(data), "--json",
    ]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["ports"]["http"] == 18123
    assert report["tls"]["certificate"]["status"] == "failed"
    hint = next(step for step in report["next_steps"] if "make_cert.py" in step)
    assert f'--config {base} --config-override "{override}" --data-root "{data}"' in hint


def test_diagnostics_without_layer_arguments_keeps_report_shape(tmp_path, monkeypatch, capsys):
    base, _, _ = layered_files(tmp_path)
    use_network(monkeypatch)
    deny_os_observations(monkeypatch)
    monkeypatch.setattr(diagnostics, "inspect_tls", lambda _, ip: {
        "selected": ip, "certificate": {"status": "failed"}, "key_pair": {"status": "ok"},
    })

    assert diagnostics.main(["--config", str(base), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert "config_layer_args" not in report
    hint = next(step for step in report["next_steps"] if "make_cert.py" in step)
    assert f"--config {base} --advertise-ip {IP} --force" in hint


def test_make_cert_writes_under_data_root(tmp_path, monkeypatch):
    base, override, data = layered_files(tmp_path)
    use_network(monkeypatch)
    monkeypatch.setattr("sys.argv", [
        "make_cert", "--config", str(base), "--config-override", str(override),
        "--data-root", str(data), "--advertise-ip", IP,
    ])

    assert make_cert.main() == 0
    assert (data / "certs" / "cert.pem").is_file()
    assert (data / "certs" / "key.pem").is_file()
