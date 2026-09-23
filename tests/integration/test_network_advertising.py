"""起動、先生情報、診断、証明書が同じ公開IPを使う（#38）。"""

import json
import re
import shutil

import pytest
from starlette.testclient import TestClient

from server.main import create_app
from server.network import InterfaceAddress
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.teacher_page import run_teacher_script
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


# ---- 担当者の選択から全案内・証明書までの一貫性と、選択設定の復元（#38 残作業） ----

VMNET8 = InterfaceAddress("VMware Network Adapter VMnet8", "192.168.74.1", True, "virtual")
SIMULATED_SCHOOL_PC = [
    VMNET8,
    InterfaceAddress("Ethernet", "192.168.1.42", True, "physical"),
    InterfaceAddress("Wi-Fi", "10.53.64.130", True, "physical"),
]


def write_config(tmp_path, advertise_ip):
    path = tmp_path / "config.yaml"
    value = "null" if advertise_ip is None else f'"{advertise_ip}"'
    path.write_text(
        f"server:\n  advertise_ip: {value}\n  cert_dir: '{(tmp_path / 'certs').as_posix()}'\n"
        "asr:\n  engine: fake\nmt:\n  engine: fake\nvad:\n  engine: energy\n  threshold: 300\n",
        encoding="utf-8",
    )
    return path


def run_startup(monkeypatch, capsys, config_path, *flags):
    """本番の起動入口 main() を通し、(起動画面の出力, 先生情報APIの応答または終了コード) を返す。

    待受は開かず、起動直後の先生情報APIをTestClientで1回だけ取得する。
    """
    from server import main

    served: dict[str, object] = {}

    async def serve(app, config, *, open_browser):
        with TestClient(app) as client:
            response = client.get("/api/teacher-info")
            served.update(status=response.status_code, body=response.json())

    monkeypatch.setattr(main, "_serve", serve)
    monkeypatch.setattr("sys.argv", ["server", "--config", str(config_path), *flags])
    try:
        main.main()
    except SystemExit as exc:
        served["exit"] = exc.code
    return capsys.readouterr().out, served


QR_PROBE = """
  await sandbox.refreshJoinInfo();
  process.stdout.write(JSON.stringify({
    qr: elements.get("qr").textContent, url: elements.get("join-url").textContent,
  }));
"""


def render_qr_text(info):
    """先生画面の表示処理へAPI応答を渡し、QRライブラリへ渡る文字列と表示URLを返す。"""
    return json.loads(run_teacher_script([{"status": 200, "body": info}], QR_PROBE))


def test_operator_choice_matches_startup_api_qr_and_certificate(tmp_path, monkeypatch, capsys):
    """社内10系＋仮想192.168系＋別の物理NIC。番号選択した実Wi-Fiだけが全案内とSANに載る。"""
    from cryptography import x509
    from scripts import make_cert
    from tests.net_guard import guard_network

    monkeypatch.setattr("server.network.list_addresses", lambda: SIMULATED_SCHOOL_PC)
    monkeypatch.setattr("builtins.input", lambda _: "3")  # 一覧の3番 = Wi-Fi
    config_path = write_config(tmp_path, None)
    wifi = "10.53.64.130"
    with guard_network() as guard:
        startup, served = run_startup(monkeypatch, capsys, config_path, "--select-network")
        assert served["status"] == 200
        info = served["body"]
        assert "3: Wi-Fi / 10.53.64.130 / physical" in startup
        assert f"公開IP     : {wifi}" in startup
        assert info["join_url"] == f"http://{wifi}:8000/?code={info['code']}"
        assert f"生徒用URL  : {info['join_url']}" in startup
        assert render_qr_text(info) == {"qr": info["join_url"], "url": info["join_url"]}

        # 起動画面が案内した証明書生成コマンドを、そのまま証明書CLIへ渡す。
        command = re.search(r'make_cert\.py --config "(.+?)" --advertise-ip (\S+) --force', startup)
        assert command and command.group(2) == wifi
        monkeypatch.setattr("sys.argv", ["make_cert", "--config", command.group(1),
                                         "--advertise-ip", command.group(2), "--force"])
        assert make_cert.main() == 0
        capsys.readouterr()
        # 再起動後、SANの一致により起動画面とAPIの先生URLが同じ実Wi-FiのHTTPSになる。
        restarted_startup, restarted = run_startup(
            monkeypatch, capsys, config_path, "--select-network")
    cert = x509.load_pem_x509_certificate((tmp_path / "certs" / "cert.pem").read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    san_ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    assert wifi in san_ips
    assert not san_ips & {"192.168.74.1", "192.168.1.42"}
    teacher_url = f"https://{wifi}:8443/teacher"
    assert restarted["body"]["teacher_url"] == teacher_url
    assert f"先生ページ : {teacher_url}" in restarted_startup
    assert f"生徒用URL  : {restarted['body']['join_url']}" in restarted_startup
    guard.assert_no_external_traffic()
    assert not guard.external_lookups


def test_restoring_saved_selection_setting_restores_previous_choice(tmp_path, monkeypatch, capsys):
    """docs/network-selection.md「元へ戻す」: 控えた設定へ戻すと、変更前と同じ選択に戻る。"""
    from tests.net_guard import guard_network

    monkeypatch.setattr("server.network.list_addresses", lambda: [
        VMNET8, InterfaceAddress("Wi-Fi", "10.53.64.130", True, "physical"),
    ])
    config_path = write_config(tmp_path, None)
    backup = tmp_path / "config.yaml.before"
    with guard_network() as guard:
        shutil.copyfile(config_path, backup)  # 手順1: 変更前の値を控える
        _, before = run_startup(monkeypatch, capsys, config_path)
        assert before["body"]["join_url"].startswith("http://10.53.64.130:8000/")

        # 手順2: 選択設定を変更する（例: 仮想NICを固定）。案内はその値に従う。
        config_path.write_text(config_path.read_text(encoding="utf-8").replace(
            "advertise_ip: null", 'advertise_ip: "192.168.74.1"'), encoding="utf-8")
        _, changed = run_startup(monkeypatch, capsys, config_path)
        assert changed["body"]["join_url"].startswith("http://192.168.74.1:8000/")

        # 手順3: 控えた値へ戻して再起動すると、変更前の自動選択に戻る。
        shutil.copyfile(backup, config_path)
        _, restored = run_startup(monkeypatch, capsys, config_path)
    assert restored["body"]["join_url"] == before["body"]["join_url"].replace(
        before["body"]["code"], restored["body"]["code"])
    guard.assert_no_external_traffic()


def test_restored_ip_no_longer_present_is_not_used(tmp_path, monkeypatch, capsys):
    """戻した固定値が現在のNICに無い（IP変更後）場合、起動せず再選択を求める。"""
    from tests.net_guard import guard_network

    monkeypatch.setattr("server.network.list_addresses", lambda: [
        VMNET8, InterfaceAddress("Wi-Fi", "10.53.64.131", True, "physical"),
    ])
    config_path = write_config(tmp_path, "10.53.64.130")
    with guard_network() as guard:
        startup, served = run_startup(monkeypatch, capsys, config_path)
        assert served == {"exit": 1}
        assert "指定IP 10.53.64.130" in startup
        assert "10.53.64.130:8000" not in startup

        monkeypatch.setattr("builtins.input", lambda _: "")  # 起動画面で選ばずEnter
        startup, served = run_startup(monkeypatch, capsys, config_path, "--select-network")
    assert served == {"exit": 1}
    assert "2: Wi-Fi / 10.53.64.131 / physical" in startup
    assert "中止" in startup
    assert "生徒用URL" not in startup
    guard.assert_no_external_traffic()
