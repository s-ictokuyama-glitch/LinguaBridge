"""NICの状態・役割による公開接続先の選択（#38）。"""
import pytest

from server.network import InterfaceAddress, choose_ip, list_addresses, select_address


def test_corporate_wifi_wins_over_virtual_192_network():
    addresses = [
        InterfaceAddress("VMnet8", "192.168.74.1", True, "virtual"),
        InterfaceAddress("Wi-Fi", "10.53.64.130", True, "physical"),
    ]
    assert select_address(addresses).ip == "10.53.64.130"


def test_multiple_physical_interfaces_require_selection():
    addresses = [
        InterfaceAddress("Ethernet", "192.168.1.42", True, "physical"),
        InterfaceAddress("Wi-Fi", "10.53.64.130", True, "physical"),
    ]
    with pytest.raises(ValueError, match="選択"):
        select_address(addresses)
    assert select_address(addresses, "10.53.64.130").name == "Wi-Fi"


@pytest.mark.parametrize("ip,up", [
    ("10.53.64.130", False), ("169.254.1.2", True), ("127.0.0.1", True),
    ("0.0.0.0", True), ("224.0.0.1", True), ("999.1.2.3", True),
])
def test_invalid_or_down_selection_is_not_silently_replaced(ip, up):
    addresses = [
        InterfaceAddress("Wi-Fi", ip, up, "physical"),
        InterfaceAddress("Ethernet", "192.168.1.42", True, "physical"),
    ]
    with pytest.raises(ValueError, match="指定IP"):
        select_address(addresses, ip)


def test_disappeared_ip_and_unknown_role_require_operator_action():
    addresses = [InterfaceAddress("Wi-Fi", "10.53.64.131", True, "unknown")]
    with pytest.raises(ValueError, match="指定IP"):
        select_address(addresses, "10.53.64.130")
    with pytest.raises(ValueError, match="選択"):
        select_address(addresses)
    assert select_address(addresses, "10.53.64.131").ip == "10.53.64.131"


def test_windows_inventory_reads_state_and_role_without_connections(monkeypatch):
    import json
    import subprocess
    from tests.net_guard import guard_network

    def inventory(args, **kwargs):
        assert args[-1].endswith("network_interfaces.ps1")
        assert kwargs["timeout"] == 10
        return subprocess.CompletedProcess(args, 0, json.dumps([
            {"name": "VMnet8", "ip": "192.168.74.1", "up": True, "role": "virtual"},
            {"name": "Wi-Fi", "ip": "10.53.64.130", "up": True, "role": "physical"},
        ]))

    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr(subprocess, "run", inventory)
    with guard_network() as guard:
        assert select_address(list_addresses()).ip == "10.53.64.130"
    assert not guard.seen
    assert not guard.resolved


def test_cim_denied_does_not_guess_physical_role(monkeypatch):
    import socket
    from types import SimpleNamespace
    from tests.net_guard import guard_network

    def denied(*args, **kwargs):
        raise PermissionError()

    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr("subprocess.run", denied)
    monkeypatch.setattr("psutil.net_if_stats", lambda: {"Wi-Fi": SimpleNamespace(isup=True)})
    monkeypatch.setattr("psutil.net_if_addrs", lambda: {
        "Wi-Fi": [SimpleNamespace(family=socket.AF_INET, address="10.53.64.130")]
    })
    with guard_network() as guard:
        addresses = list_addresses()
    assert addresses == [InterfaceAddress("Wi-Fi", "10.53.64.130", True, "unknown")]
    with pytest.raises(ValueError, match="選択"):
        select_address(addresses)
    assert not guard.seen
    assert not guard.resolved


def test_operator_selects_wifi_and_selection_is_revalidated(monkeypatch, capsys):
    addresses = [
        InterfaceAddress("Ethernet", "192.168.1.42", True, "physical"),
        InterfaceAddress("Wi-Fi", "10.53.64.130", True, "physical"),
    ]
    monkeypatch.setattr("server.network.list_addresses", lambda: addresses)
    monkeypatch.setattr("builtins.input", lambda _: "2")
    assert choose_ip(None, interactive=True) == "10.53.64.130"
    assert "Wi-Fi / 10.53.64.130" in capsys.readouterr().out

    def disconnect(_):
        addresses.pop()
        return "2"

    monkeypatch.setattr("builtins.input", disconnect)
    with pytest.raises(ValueError, match="指定IP"):
        choose_ip(None, interactive=True)
