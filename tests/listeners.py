from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
import os
import platform
import socket
import ssl
import struct
import sys

import h11
import pytest
import uvicorn
import websockets

from scripts.make_cert import generate
from server import event_loop, main
from server.asr.fake_engine import FakeASREngine
from server.mt.fake_engine import FakeTranslationEngine
from server.publication import Publication
from server.serving import ListenerSpec, TLSFiles, start_listeners
from tests.helpers import JOIN_CODE, make_ws_test_config

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
    def __init__(self, tmp_path):
        self.config = config = make_ws_test_config()
        config.server.cert_dir = str(tmp_path)
        config.server.http_port = config.server.https_port = 0
        self.publication = Publication(config.server)
        generate(config.server.cert_path(), config.server.key_path(), ip=self.publication.ip)
        self.publication = Publication(config.server)
        self.app = main.create_app(config, self.publication, asr_engine=FakeASREngine(),
            mt_engine=FakeTranslationEngine(["en", "zh"]), join_code=JOIN_CODE)
        self.exceptions = []
        self.running = None

    def specs(self):
        return [ListenerSpec("127.0.0.1", 0, lifespan=True),
                ListenerSpec("127.0.0.1", 0, tls=TLSFiles(self.publication.cert_path,
                                                       self.publication.key_path))]

    @asynccontextmanager
    async def serving(self, *, install_signals=False):
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        def observe(active_loop, context):
            self.exceptions.append(context)
            active_loop.default_exception_handler(context)
        loop.set_exception_handler(observe)
        ports = ()
        try:
            async with start_listeners(self.app, self.specs(),
                                       install_signals=install_signals) as running:
                self.running = running
                ports = running.ports
                await wait_until(lambda: self.app.state.pipeline.ready, "pipeline ready")
                yield ports
                running.stop()
                await running.wait()
        finally:
            loop.set_exception_handler(previous)
        assert not self.app.state.lesson.roster.clients
        remaining = [t for t in asyncio.all_tasks()
                     if t is not asyncio.current_task() and not t.done()]
        assert not remaining, [(t.get_name(), str(t.get_coro())) for t in remaining]
        for port in ports:
            with socket.socket() as probe:
                probe.settimeout(1)
                assert probe.connect_ex(("127.0.0.1", port)) != 0

    def tls(self):
        return ssl.create_default_context(cafile=str(self.config.server.cert_path()))

    async def assert_healthy(self, http, ports):
        for scheme, port in zip(("http", "https"), ports):
            response = await http.get(f"{scheme}://127.0.0.1:{port}/healthz")
            assert response.status_code == 200
            assert response.json() == {"status": "ok", "ready": True}
