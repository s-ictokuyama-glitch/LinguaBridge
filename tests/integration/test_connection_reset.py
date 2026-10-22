"""#42: 本番の二重待受に対する、ループバックでの実TCP/TLSリセット。

推論だけフェイクで、ソケット・TLS・Uvicorn・本番のイベントループ・アプリの後始末は実物。
ローカルでの復旧の証拠であり、現地の10054との同一性は示さない（照合は #43 F6〜F8）。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
import os
import platform
import socket
import ssl
import struct
import sys

import h11
import httpx
import pytest
import uvicorn
import websockets
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from scripts.make_cert import generate
from server import event_loop, main
from server.asr.fake_engine import FakeASREngine
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.helpers import utterance_bytes

LINGER_RST = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)


async def wait_until(predicate, description):
    try:
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    except TimeoutError:
        pytest.fail(f"timed out waiting for: {description}")


async def receive(ws, kind, **expected):
    async with asyncio.timeout(5):
        while True:
            message = json.loads(await ws.recv())
            if message.get("type") == kind:
                assert all(message.get(key) == value for key, value in expected.items()), message
                return message


def reset_socket(ws):
    """Abort without a WS close frame or TLS close_notify; ask OS to send RST."""
    raw = ws.transport.get_extra_info("socket")
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, LINGER_RST)
    # abort() cancels the pending read now but defers the close, where Proactor
    # calls shutdown(SHUT_RDWR) and so sends FIN (a normal EOF). Closing the
    # real socket in the same tick skips that shutdown and makes it an RST.
    ws.transport.abort()
    raw._sock.close()


def describe(exc):
    """Full exception text for the evidence record (type, errno, WinError, message)."""
    return {"type": type(exc).__name__, "errno": getattr(exc, "errno", None),
            "winerror": getattr(exc, "winerror", None), "text": str(exc), "repr": repr(exc)}


def runtime_versions():
    return {"python": sys.version, "platform": platform.platform(),
            "uvicorn": uvicorn.__version__, "websockets": websockets.__version__,
            "h11": h11.__version__, "stock_loop_env": os.environ.get(event_loop.STOCK_LOOP_ENV)}


def run_in_worker(scenario, timeout):
    # The production loop via the production entry point. A worker thread keeps
    # the real launcher's signal setup from replacing pytest's. timeout must be
    # below the test's pytest timeout so a hang fails here, not in teardown.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: event_loop.run(scenario())).result(timeout=timeout)


class Launcher:
    """Production app and dual listener on loopback, with observation hooks."""

    def __init__(self, tmp_path, monkeypatch):
        self.config = config = make_ws_test_config()
        config.server.cert_dir = str(tmp_path)
        config.server.advertise_ip = "127.0.0.1"
        config.server.http_port = config.server.https_port = 0
        generate(config.server.cert_path(), config.server.key_path(), ip="127.0.0.1")
        self.app = main.create_app(
            config, asr_engine=FakeASREngine(),
            mt_engine=FakeTranslationEngine(["en", "zh"]), join_code=JOIN_CODE,
        )
        self.servers = []
        self.lost = []  # (UTC time, exception or None) per server-side WS transport close
        self.exceptions = []
        original_config, original_server = uvicorn.Config, uvicorn.Server
        lost = self.lost

        def loopback_config(*args, **kwargs):
            # Same production launcher/backends, isolated binding and pytest logging.
            kwargs.update(host="127.0.0.1", log_config=None, access_log=False)
            item = original_config(*args, **kwargs)
            item.load()
            backend = item.ws_protocol_class

            class ObservedProtocol(backend):
                def connection_lost(self, exc):
                    lost.append((datetime.now(timezone.utc).isoformat(), exc))
                    return super().connection_lost(exc)

            item.ws_protocol_class = ObservedProtocol
            return item

        def observe_server(*args, **kwargs):
            server = original_server(*args, **kwargs)
            self.servers.append(server)
            return server

        monkeypatch.setattr(uvicorn, "Config", loopback_config)
        monkeypatch.setattr(uvicorn, "Server", observe_server)

    @asynccontextmanager
    async def serving(self):
        loop = asyncio.get_running_loop()

        def observe_exception(active_loop, context):
            self.exceptions.append(context)
            # Keep the usual logging, including unexpected exceptions.
            active_loop.default_exception_handler(context)

        loop.set_exception_handler(observe_exception)
        serving = asyncio.create_task(main._serve(self.app, self.config, open_browser=False))
        ports = []
        try:
            await wait_until(
                lambda: len(self.servers) == 2 and all(s.started for s in self.servers),
                "dual listener startup",
            )
            ports = [s.servers[0].sockets[0].getsockname()[1] for s in self.servers]
            yield ports
        finally:
            for server in self.servers:
                server.should_exit = True
            await asyncio.wait_for(serving, timeout=10)
            loop.set_exception_handler(None)

        # Check before asyncio.run's implicit cancellation could hide leaked tasks.
        assert not self.app.state.session.clients
        assert not self.app.state.pipeline._senders
        assert all(not s.server_state.connections and not s.server_state.tasks
                   for s in self.servers)
        remaining = [t for t in asyncio.all_tasks()
                     if t is not asyncio.current_task() and not t.done()]
        assert not remaining, [(t.get_name(), str(t.get_coro())) for t in remaining]
        for port in ports:
            with socket.socket() as probe:
                probe.settimeout(1)
                assert probe.connect_ex(("127.0.0.1", port)) != 0, "listener remains open"

    def tls(self):
        return ssl.create_default_context(cafile=str(self.config.server.cert_path()))

    async def assert_healthy(self, http, ports):
        for scheme, port in zip(("http", "https"), ports):
            response = await http.get(f"{scheme}://127.0.0.1:{port}/healthz")
            assert response.status_code == 200
            assert response.json() == {"status": "ok", "ready": True}


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    return Launcher(tmp_path, monkeypatch)


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
                        before_ids = set(app.state.session.clients)
                        closed_before = len(launcher.lost)
                        reset_at = datetime.now(timezone.utc).isoformat()
                        reset_socket(victim)
                        await wait_until(lambda: len(launcher.lost) > closed_before,
                                         "server observed reset")
                        observed_at, exc = launcher.lost[closed_before]
                        trials.append({
                            "reset_at": reset_at, "observed_at": observed_at,
                            "stage": f"{'wss' if secure else 'ws'} open, {role} joined, session live",
                            "server_exception": describe(exc) if exc else None,
                        })
                        await wait_until(
                            lambda: len(app.state.session.clients) == len(before_ids) - 1,
                            "disconnected client released",
                        )
                        removed = before_ids - set(app.state.session.clients)
                        assert len(removed) == 1
                        await wait_until(
                            lambda: not removed.intersection(app.state.pipeline._senders),
                            "disconnected sender released",
                        )
                        await launcher.assert_healthy(http, ports)
                        if role == "teacher":
                            await receive(survivor, "session", state="paused")
                            teacher = await join("teacher")
                            await receive(survivor, "session", state="live")
                        else:
                            assert app.state.session.state == "live"

                        # Existing student receives new work; newcomer replays the missed caption.
                        seq = iteration + 2
                        result = await caption(teacher, survivor, 2000, seq)
                        newcomer = await join("student", last_seq=seq - 1)
                        replay = await receive(newcomer, "caption", seq=seq, lang="en")
                        assert replay["text"] == result["text"]
                        await newcomer.close()
                        await wait_until(lambda: len(app.state.session.clients) == 2,
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
                    await wait_until(lambda: not app.state.session.clients, "all clients released")
                    await wait_until(lambda: not app.state.pipeline._senders,
                                     "all senders released")
        finally:
            for ws in clients:
                await ws.close()

    run_in_worker(scenario, timeout=50)
    record_property("reset_trials", json.dumps(trials, ensure_ascii=False))
    resets = [t for t in trials if (t["server_exception"] or {}).get("type") == "ConnectionResetError"]
    assert len(resets) == 3, "RST must reach server transport, not just a normal close"
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
            await wait_until(lambda: all(not s.server_state.connections for s in launcher.servers),
                             "reset connections released")
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
            await wait_until(lambda: not app.state.session.clients, "joined clients released")
            return refused

    refused = run_in_worker(scenario, timeout=110)
    accept_resets = [r.getMessage() for r in caplog.records if r.name == "server.event_loop"]
    record_property("accept_peer_resets", json.dumps(accept_resets, ensure_ascii=False))
    assert not refused, "a listener stopped accepting during the burst"
    assert not launcher.exceptions, "unhandled event-loop exception; inspect captured log"
