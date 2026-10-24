from dataclasses import FrozenInstanceError

import pytest
from starlette.testclient import TestClient

from server.config import ServerConfig
from server.network import InterfaceAddress
from server.publication import Publication
from server.main import create_app
from server.asr.fake_engine import FakeASREngine
from server.mt.fake_engine import FakeTranslationEngine
from tests.certificates import write_pair
from tests.helpers import make_ws_test_config

IP = "192.168.5.25"
WIFI = InterfaceAddress("Wi-Fi", IP, True, "physical")
OTHER = InterfaceAddress("Ethernet", "10.1.2.3", True, "physical")


def test_one_physical_nic_is_captured_before_first_request(tmp_path, monkeypatch):
    addresses = [WIFI, InterfaceAddress("VMnet8", "192.168.74.1", True, "virtual")]
    monkeypatch.setattr("server.network.list_addresses", lambda: addresses)
    config = ServerConfig(cert_dir=str(tmp_path))
    publication = Publication(config)
    assert publication.selected == WIFI
    assert config.advertise_ip is None
    addresses[:] = [OTHER]
    with pytest.raises(ValueError, match="再起動"):
        publication.current()


def test_ambiguous_network_requires_selection_and_explicit_ip_wins(tmp_path, monkeypatch):
    monkeypatch.setattr("server.network.list_addresses", lambda: [WIFI, OTHER])
    config = ServerConfig(cert_dir=str(tmp_path))
    with pytest.raises(ValueError, match="選択"):
        Publication(config)
    selected = Publication(config, requested_ip=OTHER.ip)
    assert selected.ip == OTHER.ip
    assert config.advertise_ip is None


@pytest.mark.parametrize("problem, expected", [
    ("valid", True), ("san", False), ("expired", False), ("key", False),
    ("missing", False),
])
def test_tls_policy_and_teacher_url_use_real_certificate_files(tmp_path, problem, expected):
    if problem != "missing":
        write_pair(tmp_path, ip=OTHER.ip if problem == "san" else IP,
                   days=-1 if problem == "expired" else 90, starts=-100)
    if problem == "key":
        write_pair(tmp_path / "other")
        (tmp_path / "key.pem").write_bytes((tmp_path / "other/key.pem").read_bytes())
    publication = Publication(ServerConfig(cert_dir=str(tmp_path), http_port=18080, https_port=18443))
    assert publication.https_available is expected
    assert publication.teacher_url == (
        f"https://{IP}:18443/teacher" if expected else "http://127.0.0.1:18080/teacher")
    assert publication.join_url("4831") == f"http://{IP}:18080/?code=4831"
    assert publication.tls["remote_trust"]["status"] == "unknown"


def test_tls_report_and_configuration_are_private_snapshots(tmp_path):
    write_pair(tmp_path)
    config = ServerConfig(cert_dir=str(tmp_path), http_port=18080, https_port=18443)
    publication = Publication(config)
    original = publication.tls
    exposed = publication.tls
    exposed["certificate"]["ip_addresses"].append("1.2.3.4")
    exposed["certificate"]["status"] = "failed"
    config.http_port = 99
    config.https_port = 100
    config.cert_dir = str(tmp_path / "elsewhere")
    write_pair(tmp_path, ip=OTHER.ip)
    assert publication.tls == original
    assert publication.https_available
    assert publication.cert_path == tmp_path / "cert.pem"
    assert publication.teacher_url == f"https://{IP}:18443/teacher"
    assert publication.http_url() == f"http://{IP}:18080/"
    with pytest.raises(FrozenInstanceError):
        publication.http_port = 1


def test_teacher_info_keeps_tls_snapshot_after_certificate_swap(tmp_path):
    config = make_ws_test_config()
    config.server.cert_dir = str(tmp_path)
    write_pair(tmp_path)
    publication = Publication(config.server)
    app = create_app(config, publication, asr_engine=FakeASREngine(),
                     mt_engine=FakeTranslationEngine(["en", "zh"]))
    with TestClient(app) as client:
        before = client.get("/api/teacher-info").json()
        write_pair(tmp_path, ip=OTHER.ip)
        after = client.get("/api/teacher-info").json()
    assert after == before
    assert after["tls"] == publication.tls


def test_failure_stays_fixed_after_network_returns(tmp_path, monkeypatch):
    addresses = [WIFI]
    monkeypatch.setattr("server.network.list_addresses", lambda: addresses)
    publication = Publication(ServerConfig(cert_dir=str(tmp_path)))
    addresses.clear()
    with pytest.raises(ValueError) as first:
        publication.current()
    addresses.append(WIFI)
    with pytest.raises(ValueError) as second:
        publication.current()
    assert str(first.value) == str(second.value)
