"""#45: サーバー・診断・証明書スクリプトを上書き設定とデータルートつきで起動できる。"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import sys
from http.client import HTTPConnection
from html import escape, unescape

import pytest
from starlette.testclient import TestClient

from scripts import make_cert
from server import diagnostics, main
from server.launch_commands import ROOT
from server.network import InterfaceAddress
from tests.certificates import IP, write_pair


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

    async def serve(app, *, open_browser):
        served["config"] = app.state.config
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

    async def serve(app, *, open_browser):
        pass

    monkeypatch.setattr(main, "_serve", serve)
    main.main()
    output = capsys.readouterr().out

    assert f'server.diagnostics --config "{base}" --advertise-ip {IP} --json' in output
    assert "--config-override" not in output and "--data-root" not in output


@pytest.mark.parametrize("bundled", [False, True])
@pytest.mark.parametrize("layered", [False, True])
@pytest.mark.parametrize("unavailable", [False, True])
def test_help_diagnostics_preserves_startup_context(
    tmp_path, monkeypatch, capsys, bundled, layered, unavailable,
):
    folder = tmp_path / "school & classroom"
    folder.mkdir()
    base, override, data = layered_files(folder)
    write_pair(data / "certs")
    use_network(monkeypatch)
    if bundled:
        run_as_distribution(monkeypatch)
    arguments = ["--config", str(base)]
    if layered:
        arguments += ["--config-override", str(override), "--data-root", str(data)]
    monkeypatch.setattr("sys.argv", ["server", *arguments, "--advertise-ip", IP])
    responses = []

    async def serve(app, *, open_browser):
        if unavailable:
            monkeypatch.setattr("server.network.list_addresses", lambda: [])
        with TestClient(app) as client:
            responses.append(client.get("/connection-help"))

    monkeypatch.setattr(main, "_serve", serve)
    main.main()
    output = capsys.readouterr().out
    response = responses[0]
    assert response.status_code == (503 if unavailable else 200)
    assert response.headers["cache-control"] == "no-store"
    prefix = f"{BUNDLED} -m server.diagnostics" if bundled else r".venv\Scripts\python -m server.diagnostics"
    config_args = layered_args(folder) if layered else f'--config "{base}"'
    command = f"{prefix} {config_args} --advertise-ip {IP} --json"
    assert f"<code>{escape(command)} &gt; connection-report.json</code>" in response.text
    assert f"HTTPS再確認: 再起動後、{command}" in output
    assert "コマンドプロンプト" in response.text
    assert "起動時" in response.text
    assert "start.bat --diagnose" in response.text
    assert "{{" not in response.text
    if bundled:
        assert ".venv" not in response.text
    if unavailable:
        assert "https://" not in response.text

    saved = next(unescape(code) for code in re.findall(r"<code>(.*?)</code>", response.text)
                 if code.endswith("&gt; connection-report.json"))
    diag_args = split(saved.removesuffix(" > connection-report.json"))
    use_network(monkeypatch)
    deny_os_observations(monkeypatch)
    assert diagnostics.main(diag_args[diag_args.index("--config"):]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ports"]["http"] == (18123 if layered else 8000)
    assert report["network"]["selected_ip"] == IP
    assert report["record"]["config_file"] == str(base)
    if layered:
        assert report["tls"]["certificate"]["status"] == "ok"
        assert report["config_layer_args"] == f'--config-override "{override}" --data-root "{data}"'
    else:
        assert "config_layer_args" not in report


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
    assert f'--config "{base}" --config-override "{override}" --data-root "{data}"' in hint


def test_diagnostics_without_layer_arguments_keeps_report_shape(tmp_path, monkeypatch, capsys):
    base, _, _ = layered_files(tmp_path)
    use_network(monkeypatch)
    deny_os_observations(monkeypatch)
    monkeypatch.setattr(diagnostics, "inspect_tls", lambda _, ip, **kwargs: {
        "selected": ip, "certificate": {"status": "failed"}, "key_pair": {"status": "ok"},
    })

    assert diagnostics.main(["--config", str(base), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert "config_layer_args" not in report
    hint = next(step for step in report["next_steps"] if "make_cert.py" in step)
    assert f'--config "{base}" --advertise-ip {IP} --force' in hint


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


# #51: 配布モード（同梱の python.exe で実行中）では .venv ではなく同梱版を案内する。
# リポジトリのパスに空白は無い前提（空白時の引用は tests/unit/test_launch_commands.py）
BUNDLED = ROOT / "python" / "python.exe"
RUNNING_PYTHON = sys.executable  # 配布モードの試験で sys.executable を差し替える前の実体


def layered_args(tmp_path) -> str:
    """layered_files の三層を、案内の書式（引用符つき）で並べたもの。"""
    base, override, data = tmp_path / "app" / "config.yaml", tmp_path / "data" / "config.yaml", tmp_path / "data"
    return f'--config "{base}" --config-override "{override}" --data-root "{data}"'


def run_as_distribution(monkeypatch):
    monkeypatch.setattr("sys.executable", str(BUNDLED))


def test_server_in_distribution_mode_guides_bundled_python(tmp_path, monkeypatch, capsys):
    run_as_distribution(monkeypatch)
    output, data = startup_output(tmp_path, monkeypatch, capsys)  # 証明書が無いので再発行の案内も出る

    config_args = layered_args(tmp_path)
    script = ROOT / "scripts" / "make_cert.py"
    assert f'{BUNDLED} {script} {config_args} --advertise-ip {IP} --force' in output
    assert f'{BUNDLED} -m server.diagnostics {config_args} --advertise-ip {IP} --json' in output
    assert ".venv" not in output


def test_diagnostics_in_distribution_mode_guides_bundled_python(tmp_path, monkeypatch, capsys):
    base, override, data = layered_files(tmp_path)
    write_pair(data / "certs", ip="192.168.1.35")
    use_network(monkeypatch)
    deny_os_observations(monkeypatch)
    run_as_distribution(monkeypatch)

    assert diagnostics.main([
        "--config", str(base), "--config-override", str(override),
        "--data-root", str(data), "--json",
    ]) == 0
    report = json.loads(capsys.readouterr().out)

    hint = next(step for step in report["next_steps"] if "make_cert.py" in step)
    config_args = layered_args(tmp_path)
    script = ROOT / "scripts" / "make_cert.py"
    assert f'{BUNDLED} {script} {config_args} --advertise-ip {IP} --force' in hint
    assert ".venv" not in hint


def test_make_cert_in_distribution_mode_guides_bundled_python(tmp_path, monkeypatch, capsys):
    base, override, data = layered_files(tmp_path)
    use_network(monkeypatch)
    run_as_distribution(monkeypatch)
    monkeypatch.setattr("sys.argv", [
        "make_cert", "--config", str(base), "--config-override", str(override),
        "--data-root", str(data), "--advertise-ip", IP,
    ])

    assert make_cert.main() == 0
    output = capsys.readouterr().out

    config_args = layered_args(tmp_path)
    assert f'{BUNDLED} -m server.diagnostics {config_args} --advertise-ip {IP} --json' in output
    assert ".venv" not in output


def guided_commands(output: str) -> tuple[str, str]:
    """起動画面から、証明書の再発行と HTTPS 再確認の案内コマンド行を取り出す。"""
    lines = [line.strip() for line in output.splitlines()]
    make = next(line for line in lines if "make_cert.py" in line and line.endswith("--force"))
    diag = next(line for line in lines if "server.diagnostics" in line).split("再起動後、", 1)[1]
    return make, diag


def startup_output(tmp_path, monkeypatch, capsys):
    base, override, data = layered_files(tmp_path)
    use_network(monkeypatch)
    monkeypatch.setattr("sys.argv", [
        "server", "--config", str(base), "--config-override", str(override),
        "--data-root", str(data),
    ])

    async def serve(app, *, open_browser):
        pass

    monkeypatch.setattr(main, "_serve", serve)
    main.main()
    return capsys.readouterr().out, data


def split(command: str) -> list[str]:
    return [part.strip('"') for part in shlex.split(command, posix=False)]


def launches(command: str, *, cwd, env=None) -> bool:
    """案内どおりのコマンド行が、その場所から起動して引数を受け付けるか（--help で即終了）。"""
    done = subprocess.run(f"{command} --help", shell=True, cwd=cwd, env=env,
                          capture_output=True, timeout=15)
    return done.returncode == 0


def assert_arguments_reach_the_same_data(make, diag, data, monkeypatch, capsys):
    """案内の引数どおりに実行すると、data 配下の証明書を作り、診断も同じ証明書を見る。"""
    make_args = split(make)
    monkeypatch.setattr("sys.argv", ["make_cert", *make_args[make_args.index("--config"):]])
    assert make_cert.main() == 0
    assert (data / "certs" / "cert.pem").is_file()

    deny_os_observations(monkeypatch)
    capsys.readouterr()
    diag_args = split(diag)
    assert diagnostics.main(diag_args[diag_args.index("--config"):]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ports"]["http"] == 18123
    assert report["tls"]["certificate"]["status"] == "ok"


@pytest.mark.skipif(sys.platform != "win32", reason="案内は Windows のコマンドプロンプト向け")
def test_distribution_guidance_runs_as_printed_from_any_folder(tmp_path, monkeypatch, capsys):
    run_as_distribution(monkeypatch)
    output, data = startup_output(tmp_path, monkeypatch, capsys)
    make, diag = guided_commands(output)

    # 開発機に同梱版は無いので、実行中のインタープリターで代わりに起動する。同梱版の ._pth は
    # app を sys.path に入れる（scripts/build_package.py）ため、PYTHONPATH で同じ状態にする。
    env = os.environ | {"PYTHONPATH": str(ROOT)}
    stand_in = f'"{RUNNING_PYTHON}"'
    assert make.startswith(f"{BUNDLED} ") and diag.startswith(f"{BUNDLED} ")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert launches(make.replace(str(BUNDLED), stand_in, 1), cwd=elsewhere, env=env)
    assert launches(diag.replace(str(BUNDLED), stand_in, 1), cwd=elsewhere, env=env)
    assert_arguments_reach_the_same_data(make, diag, data, monkeypatch, capsys)


@pytest.mark.skipif(not (ROOT / ".venv" / "Scripts" / "python.exe").is_file(),
                    reason="開発モードの .venv が無い")
def test_development_guidance_runs_as_printed_from_repository_root(tmp_path, monkeypatch, capsys):
    output, data = startup_output(tmp_path, monkeypatch, capsys)
    make, diag = guided_commands(output)

    assert make.startswith(r".venv\Scripts\python scripts\make_cert.py ")
    assert launches(make, cwd=ROOT)
    assert launches(diag, cwd=ROOT)
    assert_arguments_reach_the_same_data(make, diag, data, monkeypatch, capsys)
