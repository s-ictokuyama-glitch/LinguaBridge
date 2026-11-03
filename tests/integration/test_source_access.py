import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.helpers import JOIN_CODE
from tests.listeners import run_in_worker


@pytest.mark.parametrize("ip", ["8.8.8.8", "2606:4700::1111", "invalid", None])
def test_untrusted_source_is_refused_before_routing(app, caplog, ip):
    spoofed = {"X-Forwarded-For": "127.0.0.1"}
    address = (ip, 50000) if ip is not None else None
    with TestClient(app, client=address, headers=spoofed) as client:
        for path in ("/", "/api/config", "/static/teacher.js"):
            assert client.get(path).status_code == 403
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect("/ws"):
                pytest.fail("WebSocket was accepted")
        assert rejected.value.code == 1008
    assert str(ip) in caplog.text


@pytest.mark.parametrize("ip", [
    "10.53.69.8", "172.16.1.2", "192.168.1.2", "100.64.1.2",
    "127.0.0.1", "169.254.1.2", "::1", "fe80::1", "203.0.113.5",
])
def test_local_source_can_load_pages_and_join(app, ip):
    with TestClient(app, client=(ip, 50000)) as client:
        for path in ("/", "/api/config", "/static/teacher.js"):
            assert client.get(path).status_code == 200
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "join", "role": "student", "code": JOIN_CODE, "lang": "en"})
            assert ws.receive_json()["type"] == "joined"


def test_production_listener_ignores_forwarded_source(launcher):
    async def scenario():
        async with launcher.serving() as (port, _):
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(
                    f"http://127.0.0.1:{port}/ready",
                    headers={"X-Forwarded-For": "8.8.8.8"},
                )
            assert response.status_code == 200

    run_in_worker(scenario, timeout=10)
