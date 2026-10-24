"""#42: 本番の二重待受に対する、ループバックでの実TCP/TLSリセット。

推論だけフェイクで、ソケット・TLS・Uvicorn・本番のイベントループ・アプリの後始末は実物。
ローカルでの復旧の証拠であり、現地の10054との同一性は示さない（照合は #43 F6〜F8）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import socket

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from tests.helpers import JOIN_CODE, utterance_bytes
from tests.listeners import (LINGER_RST, wait_until, receive, reset_socket,
                             runtime_versions, run_in_worker)


@pytest.mark.timeout(60)
@pytest.mark.parametrize("secure", [False, True], ids=["ws", "wss"])
@pytest.mark.parametrize("role", ["student", "teacher"])
def test_reset_preserves_service_and_releases_resources(
    launcher, record_property, caplog, secure, role
):
    app = launcher.app
    record_property("runtime", json.dumps(runtime_versions(), ensure_ascii=False))
    trials = []

    async def scenario():
        record_property("event_loop", type(asyncio.get_running_loop()).__name__)
        clients = []
        try:
            async with launcher.serving() as ports:
                tls = launcher.tls()
                base = f"{'https' if secure else 'http'}://127.0.0.1:{ports[int(secure)]}"
                uri = base.replace("https:", "wss:").replace("http:", "ws:") + "/ws"
                options = {"ssl": tls} if secure else {}

                async def join(client_role, last_seq=None):
                    ws = await connect(uri, origin=base, proxy=None, open_timeout=3,
                                       close_timeout=1, **options)
                    clients.append(ws)
                    payload = {"type": "join", "role": client_role, "code": JOIN_CODE}
                    if client_role == "student":
                        payload["lang"] = "en"
                    if last_seq is not None:
                        payload["last_seq"] = last_seq
                    await ws.send(json.dumps(payload))
                    await receive(ws, "joined")
                    return ws

                async def caption(teacher, student, key, seq):
                    for data in utterance_bytes(key):
                        await teacher.send(data)
                    result = await receive(student, "caption", seq=seq, lang="en")
                    assert result["text"] == "[en] " + result["ja"]
                    return result

                async with httpx.AsyncClient(verify=tls, trust_env=False, timeout=3) as http:
                    await launcher.assert_healthy(http, ports)
                    teacher = await join("teacher")
                    survivor = await join("student")
                    await teacher.send(json.dumps({"type": "control", "action": "start"}))
                    await receive(survivor, "session", state="live")
                    await caption(teacher, survivor, 1000, 1)

                    # Three resets exercise accumulation as well as one recovery.
                    for iteration in range(3):
                        victim = await join("student") if role == "student" else teacher
                        before_ids = {c.id for c in app.state.lesson.roster.clients}
                        reset_at = datetime.now(timezone.utc).isoformat()
                        reset_socket(victim)
                        trials.append({
                            "reset_at": reset_at,
                            "stage": f"{'wss' if secure else 'ws'} open, {role} joined, session live",
                        })
                        await wait_until(
                            lambda: len(app.state.lesson.roster.clients) == len(before_ids) - 1,
                            "disconnected client released",
                        )
                        removed = before_ids - {c.id for c in app.state.lesson.roster.clients}
                        assert len(removed) == 1
                        await launcher.assert_healthy(http, ports)
                        if role == "teacher":
                            await receive(survivor, "session", state="paused")
                            teacher = await join("teacher")
                            await receive(survivor, "session", state="live")
                        else:
                            assert app.state.lesson.state == "live"

                        # Existing student receives new work; newcomer replays the missed caption.
                        seq = iteration + 2
                        result = await caption(teacher, survivor, 2000, seq)
                        newcomer = await join("student", last_seq=seq - 1)
                        replay = await receive(newcomer, "caption", seq=seq, lang="en")
                        assert replay["text"] == result["text"]
                        await newcomer.close()
                        await wait_until(lambda: len(app.state.lesson.roster.clients) == 2,
                                         "normal close")

                    # Origin rejection still works after reset; permitted joins above must work too.
                    with pytest.raises(InvalidStatus) as rejected:
                        async with connect(uri, origin="https://unrelated.invalid", proxy=None,
                                           open_timeout=3, **options):
                            pass
                    assert rejected.value.response.status_code == 403

                    await teacher.send(json.dumps({"type": "control", "action": "end"}))
                    await receive(survivor, "session", state="ended")
                    await teacher.close()
                    await survivor.close()
                    await wait_until(lambda: not app.state.lesson.roster.clients, "all clients released")
        finally:
            for ws in clients:
                await ws.close()

    run_in_worker(scenario, timeout=50)
    record_property("reset_trials", json.dumps(trials, ensure_ascii=False))
    assert len(trials) == 3
    assert not launcher.exceptions, "unhandled event-loop exception; inspect captured log"
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")]


def send_then_reset(port, payload):
    """Connect, send, and RST at once: the peer is gone before AcceptEx completes."""
    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, LINGER_RST)
        sock.sendall(payload)


@pytest.mark.timeout(120)
def test_reset_before_accept_keeps_both_listeners(launcher, record_property, caplog):
    # Locally reproduced on the stock Proactor loop: one such reset ended with
    # "Accept failed on a socket" (WinError 64) and the HTTP listener refused
    # every later client. The race is timing dependent, so this is a burst.
    attempts = 200
    record_property("runtime", json.dumps(runtime_versions(), ensure_ascii=False))
    app = launcher.app
    caplog.set_level(logging.WARNING, logger="server.event_loop")

    async def scenario():
        record_property("event_loop", type(asyncio.get_running_loop()).__name__)
        async with launcher.serving() as ports:
            http_port, https_port = ports
            ws_request = (
                f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{http_port}\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                f"Sec-WebSocket-Version: 13\r\nOrigin: http://127.0.0.1:{http_port}\r\n\r\n"
            ).encode()
            partial_client_hello = b"\x16\x03\x01\x00\x50" + b"\x01" * 20

            def burst():
                failures = []
                for _ in range(attempts):
                    for port, payload in ((http_port, ws_request),
                                          (https_port, partial_client_hello)):
                        try:
                            send_then_reset(port, payload)
                        except OSError as exc:
                            failures.append((port, repr(exc)))
                return failures

            started_at = datetime.now(timezone.utc).isoformat()
            refused = await asyncio.to_thread(burst)
            record_property("burst", json.dumps({
                "started_at": started_at, "ended_at": datetime.now(timezone.utc).isoformat(),
                "stage": "before accept: connect, send WS request / partial ClientHello, RST",
                "attempts_per_listener": attempts,
            }))
            tls = launcher.tls()
            async with httpx.AsyncClient(verify=tls, trust_env=False, timeout=3) as http:
                await launcher.assert_healthy(http, ports)
            for scheme, port, options in (("http", http_port, {}),
                                          ("https", https_port, {"ssl": tls})):
                base = f"{scheme}://127.0.0.1:{port}"
                async with connect(base.replace("http", "ws", 1) + "/ws", origin=base,
                                   proxy=None, open_timeout=3, **options) as ws:
                    await ws.send(json.dumps({"type": "join", "role": "student",
                                              "code": JOIN_CODE, "lang": "en"}))
                    await receive(ws, "joined")
            await wait_until(lambda: not app.state.lesson.roster.clients, "joined clients released")
            return refused

    refused = run_in_worker(scenario, timeout=110)
    accept_resets = [r.getMessage() for r in caplog.records if r.name == "server.event_loop"]
    record_property("accept_peer_resets", json.dumps(accept_resets, ensure_ascii=False))
    assert not refused, "a listener stopped accepting during the burst"
    assert not launcher.exceptions, "unhandled event-loop exception; inspect captured log"
