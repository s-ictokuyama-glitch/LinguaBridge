"""#50: 相手側リセットの後でも、待受を閉じる・Ctrl+C で終了する。

現地（#43、社内Wi-Fi）では、接続の後始末で shutdown() が WinError 10054 を上げ、
asyncio の Server が接続を数え続けたため、Uvicorn の終了処理が戻らなかった。
ループバックでは shutdown() は失敗しないので、RST で閉じた実接続の shutdown() だけに
その失敗を注入する。ソケット・TLS・Uvicorn・本番のイベントループは実物。
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import inspect
import logging
import signal
import socket
import ssl
import sys
import textwrap
import time

import pytest

from scripts.make_cert import generate
from server import event_loop, serving
from tests.listeners import LINGER_RST, wait_until

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Proactor only")

RESET_ERRORS = {
    "WSAECONNRESET": lambda: ConnectionResetError(errno.ECONNRESET, "reset", None, 10054),
    "WSAECONNABORTED": lambda: ConnectionAbortedError(errno.ECONNABORTED, "aborted", None, 10053),
    "ERROR_NETNAME_DELETED": lambda: OSError(errno.EINVAL, "netname deleted", None, 64),
}


class FailingShutdown:
    """shutdown() of accepted sockets on `port` raises, as observed on site after a peer RST."""

    def __init__(self, monkeypatch, make_error):
        self.port = None
        self.raised = []
        original = socket.socket.shutdown
        failing = self

        def shutdown(sock, how):
            if failing.port is not None and sock.getsockname()[1] == failing.port:
                error = make_error()
                failing.raised.append(error)
                raise error
            return original(sock, how)

        monkeypatch.setattr(socket.socket, "shutdown", shutdown)


def connect_then_reset(port, tls=None):
    sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, LINGER_RST)
    if tls is not None:
        sock = tls.wrap_socket(sock, server_hostname="127.0.0.1")
    sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    time.sleep(0.05)  # let the server read it, so the RST hits an established transport
    sock.close()


def tls_contexts(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    generate(cert, key, ip="127.0.0.1")
    server_side = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_side.load_cert_chain(cert, key)
    return server_side, ssl.create_default_context(cafile=str(cert))


async def close_after_reset(failing, server_tls=None, client_tls=None):
    """Serve, have one client RST, then close the server; report whether it finished."""
    loop = asyncio.get_running_loop()
    handled = []
    loop.set_exception_handler(lambda _loop, context: handled.append(context))

    async def idle(reader, writer):
        with contextlib.suppress(ConnectionError):  # the read itself sees the RST
            await reader.read()
        writer.close()

    server = await asyncio.start_server(idle, "127.0.0.1", 0, ssl=server_tls)
    failing.port = server.sockets[0].getsockname()[1]
    await asyncio.to_thread(connect_then_reset, failing.port, client_tls)
    await wait_until(lambda: failing.raised, "server-side cleanup of the reset connection")
    await asyncio.sleep(0.05)  # let the rest of that cleanup run
    server.close()
    try:
        await asyncio.wait_for(server.wait_closed(), timeout=3)
        closed = True
    except TimeoutError:
        closed = False
    return type(loop), closed, server._active_count, handled


@pytest.mark.timeout(30)
@pytest.mark.parametrize("secure", [False, True], ids=["tcp", "tls"])
@pytest.mark.parametrize("error", RESET_ERRORS.values(), ids=RESET_ERRORS.keys())
def test_failed_shutdown_after_peer_reset_still_detaches(monkeypatch, tmp_path, caplog,
                                                          secure, error):
    failing = FailingShutdown(monkeypatch, error)
    server_tls, client_tls = tls_contexts(tmp_path) if secure else (None, None)
    with caplog.at_level(logging.DEBUG, logger="server.event_loop"):
        loop_type, closed, active, handled = event_loop.run(
            close_after_reset(failing, server_tls, client_tls))

    assert loop_type is not asyncio.ProactorEventLoop
    assert closed, "server.wait_closed() must return: the reset connection is detached"
    assert active == 0
    assert not handled, "a per-connection reset is not reported as an unhandled error"
    # Logged with its WinError at DEBUG: routine browser RSTs must not flood the teacher console.
    winerror = str(failing.raised[0].winerror)
    logged = [r for r in caplog.records if r.name == "server.event_loop" and winerror in r.getMessage()]
    assert logged
    assert all(r.levelno == logging.DEBUG for r in logged)


@pytest.mark.timeout(30)
def test_other_shutdown_errors_stay_visible(monkeypatch):
    # Not a peer reset: keep the stock behaviour, reported through the exception handler.
    failing = FailingShutdown(monkeypatch, lambda: OSError(errno.EINVAL, "other", None, 10022))
    _, closed, _, handled = event_loop.run(close_after_reset(failing))

    assert not closed
    assert [type(c.get("exception")) for c in handled] == [OSError]


@pytest.mark.timeout(30)
def test_rollback_switch_restores_stock_transport(monkeypatch):
    # LINGUABRIDGE_STOCK_EVENT_LOOP=1 brings the stock transport back, and with it
    # this defect; main._serve's graceful-shutdown limit still ends the process.
    monkeypatch.setenv(event_loop.STOCK_LOOP_ENV, "1")
    failing = FailingShutdown(monkeypatch, RESET_ERRORS["WSAECONNRESET"])
    loop_type, closed, active, handled = event_loop.run(close_after_reset(failing))

    assert loop_type is asyncio.ProactorEventLoop
    assert not closed and active == 1
    assert [type(c.get("exception")) for c in handled] == [ConnectionResetError]


# 写し元のソース（CPython 3.12.10 と 3.14.2。_call_connection_lost は 3.13 から
# server._detach(self) になる点だけが違う）。ランタイム更新で変わったら
# server/event_loop.py の写しを見直し、ここを更新する。
STDLIB_COPIES = {
    "_ProactorBasePipeTransport._call_connection_lost": {
        "f1acff07581312a6d5732257ff76a4c76a29eb44688d1bacb16d4ca7fc9eba1d",  # 3.12.10
        "433d6dae1c14d020ca7baa6b449a4a1ce50751f9514baef8770f4ced4e8c780d",  # 3.14.2
    },
    "BaseProactorEventLoop._make_ssl_transport": {
        "18f5395eb3c6ee8ddd5e1b94786aeccbfe1c403f81bb6ba2dd653b287d72e67d",  # 3.12.10, 3.14.2
    },
}


def test_stdlib_originals_unchanged():
    from asyncio import proactor_events

    originals = (proactor_events._ProactorBasePipeTransport._call_connection_lost,
                 proactor_events.BaseProactorEventLoop._make_ssl_transport)
    digests = {
        f.__qualname__: hashlib.sha256(textwrap.dedent(inspect.getsource(f)).encode()).hexdigest()
        for f in originals
    }
    unknown = {name: digest for name, digest in digests.items()
               if digest not in STDLIB_COPIES[name]}
    assert not unknown, "asyncio changed: re-review server/event_loop.py copies"


# --- Ctrl+C on the production dual listener ------------------------------------------


def serve_until_one_sigint(launcher, failing):
    """Run main._serve on the main thread, RST one HTTPS client, then press Ctrl+C once.

    Returns seconds from Ctrl+C until _serve returned, and until lifespan shutdown began.
    """
    stopped = []  # time.monotonic() when pipeline.stop was called
    pipeline = launcher.app.state.pipeline
    original_stop = pipeline.stop

    async def observed_stop():
        stopped.append(time.monotonic())
        await original_stop()

    pipeline.stop = observed_stop

    async def scenario():
        async with launcher.serving(install_signals=True) as ports:
            failing.port = ports[1]
            await asyncio.to_thread(connect_then_reset, ports[1], launcher.tls())
            await wait_until(lambda: failing.raised, "server-side cleanup of reset connection")
            await asyncio.sleep(0.05)
            started = time.monotonic()
            signal.raise_signal(signal.SIGINT)
            await asyncio.wait_for(launcher.running.wait(), timeout=15)
        return ports, started, time.monotonic() - started

    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        ports, started, elapsed = event_loop.run(scenario())
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    for port in ports:
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0, "listener remains open"
    assert stopped, "lifespan shutdown (pipeline.stop) must run"
    return elapsed, stopped[0] - started


@pytest.mark.timeout(60)
def test_one_ctrl_c_stops_both_listeners_after_reset(launcher, monkeypatch):
    failing = FailingShutdown(monkeypatch, RESET_ERRORS["WSAECONNRESET"])
    elapsed, _ = serve_until_one_sigint(launcher, failing)
    assert elapsed < serving.GRACEFUL_SHUTDOWN_S, "finished without relying on the backstop"


@pytest.mark.timeout(60)
def test_rollback_switch_still_stops_within_graceful_limit(launcher, monkeypatch):
    monkeypatch.setenv(event_loop.STOCK_LOOP_ENV, "1")
    monkeypatch.setattr(serving, "GRACEFUL_SHUTDOWN_S", 2)
    failing = FailingShutdown(monkeypatch, RESET_ERRORS["WSAECONNRESET"])
    elapsed, stop_began = serve_until_one_sigint(launcher, failing)
    assert elapsed >= 2, "the stock transport leaves the connection counted until the limit"
    assert 2 <= stop_began < 4
