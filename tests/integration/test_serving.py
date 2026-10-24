import asyncio
from contextlib import asynccontextmanager
import signal
import socket
import time

from fastapi import FastAPI, Request
import httpx
import pytest

from server import event_loop, serving
from server.serving import ListenerSpec, TLSFiles, start_listeners
from tests.certificates import write_pair


def assert_closed(ports):
    for port in ports:
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0


def application(events):
    @asynccontextmanager
    async def lifespan(app):
        events.append("start")
        yield {"token": "shared"}
        events.append("stop")

    app = FastAPI(lifespan=lifespan)

    @app.get("/")
    async def read(request: Request):
        return {"state": request.state.token}
    return app


def test_real_http_https_callback_single_lifespan_and_idempotent_stop(tmp_path):
    write_pair(tmp_path)
    events = []
    specs = [ListenerSpec("127.0.0.1", 0, lifespan=True),
             ListenerSpec("127.0.0.1", 0, tls=TLSFiles(tmp_path / "cert.pem", tmp_path / "key.pem"))]
    async def scenario():
        callback_started = asyncio.Event()
        callback_done = asyncio.Event()
        async def callback():
            callback_started.set()
            try:
                await asyncio.Future()
            finally:
                callback_done.set()
        async with start_listeners(application(events), specs, install_signals=False,
                                   on_ready=callback) as running:
            await callback_started.wait()
            assert len(set(running.ports)) == 2
            async with httpx.AsyncClient(verify=False, trust_env=False) as client:
                for scheme, port in zip(("http", "https"), running.ports):
                    response = await client.get(f"{scheme}://127.0.0.1:{port}/")
                    assert response.json() == {"state": "shared"}
            running.stop()
            running.stop()
            await asyncio.wait_for(running.wait(), 3)
            await running.wait()
        assert callback_done.is_set()
        assert_closed(running.ports)
    event_loop.run(scenario())
    assert events == ["start", "stop"]


@pytest.mark.parametrize("failure", ["bind", "tls", "startup", "callback"])
def test_failure_cleans_listeners_and_restores_signal_handlers(tmp_path, failure):
    events = []
    ports = []
    app = application(events)
    async def broken_callback():
        raise RuntimeError("callback failed")
    if failure == "startup":
        @asynccontextmanager
        async def broken_lifespan(app):
            raise RuntimeError("startup failed")
            yield
        app = FastAPI(lifespan=broken_lifespan)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        reserved = reservation.getsockname()[1]
    ports.append(reserved)
    specs = [ListenerSpec("127.0.0.1", reserved, lifespan=True)]
    if failure == "tls":
        specs.append(ListenerSpec("127.0.0.1", 0,
                                  tls=TLSFiles(tmp_path / "absent.pem", tmp_path / "absent.key")))
    if failure == "bind":
        specs.append(ListenerSpec("127.0.0.1", reserved))
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    async def scenario():
        async with start_listeners(app, specs,
                                   on_ready=broken_callback if failure == "callback" else None) as running:
            ports.extend(running.ports)
            await running.wait()
    with pytest.raises((OSError, RuntimeError)):
        event_loop.run(scenario())
    assert {sig: signal.getsignal(sig) for sig in before} == before
    assert_closed(ports)
    assert events == (["start", "stop"] if failure == "callback" else [])


def test_unresponsive_request_drains_with_limit_and_finalization_completes(monkeypatch):
    monkeypatch.setattr(serving, "GRACEFUL_SHUTDOWN_S", 0.15)
    events = []
    entered = asyncio.Event()
    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.sleep(0.25)
        events.append("finalized")
    app = FastAPI(lifespan=lifespan)
    @app.get("/")
    async def stall():
        entered.set()
        await asyncio.Future()
    async def scenario():
        async with start_listeners(app, [ListenerSpec("127.0.0.1", 0, lifespan=True)],
                                   install_signals=False) as running:
            reader, writer = await asyncio.open_connection("127.0.0.1", running.ports[0])
            writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            await entered.wait()
            started = time.monotonic()
            running.stop()
            await asyncio.wait_for(running.wait(), 2)
            writer.close()
            await writer.wait_closed()
        assert time.monotonic() - started < 2
        assert_closed(running.ports)
    event_loop.run(scenario())
    assert events == ["finalized"]


def test_no_signals_option_leaves_handlers_untouched():
    async def scenario():
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        async with start_listeners(FastAPI(), [ListenerSpec("127.0.0.1", 0)],
                                   install_signals=False) as running:
            assert {sig: signal.getsignal(sig) for sig in before} == before
            running.stop()
            await running.wait()
    event_loop.run(scenario())


def test_two_sigints_stop_both_listeners_without_skipping_finalization():
    events = []
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    async def scenario():
        async with start_listeners(application(events), [ListenerSpec("127.0.0.1", 0, lifespan=True),
                                                        ListenerSpec("127.0.0.1", 0)]) as running:
            signal.raise_signal(signal.SIGINT)
            signal.raise_signal(signal.SIGINT)
            await asyncio.wait_for(running.wait(), 3)
        assert_closed(running.ports)
    event_loop.run(scenario())
    assert events == ["start", "stop"]
    assert {sig: signal.getsignal(sig) for sig in before} == before


@pytest.mark.parametrize("stop_mode", ["stop", "context", "cancel", "signal"])
def test_shutdown_closes_connection_before_tls_handshake(tmp_path, monkeypatch, stop_mode):
    monkeypatch.setattr(serving, "GRACEFUL_SHUTDOWN_S", 0.2)
    write_pair(tmp_path)
    events = []
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    async def scenario():
        reader = writer = None

        async def owner():
            nonlocal reader, writer
            async with start_listeners(application(events), [
                ListenerSpec("127.0.0.1", 0, lifespan=True,
                             tls=TLSFiles(tmp_path / "cert.pem", tmp_path / "key.pem"))
            ]) as running:
                reader, writer = await asyncio.open_connection("127.0.0.1", running.ports[0])
                await asyncio.sleep(0.05)
                if stop_mode == "cancel":
                    asyncio.current_task().cancel()
                    await running.wait()
                elif stop_mode == "signal":
                    signal.raise_signal(signal.SIGINT)
                    await running.wait()
                elif stop_mode == "stop":
                    running.stop()
                    await running.wait()
            assert_closed(running.ports)

        try:
            task = asyncio.create_task(owner())
            if stop_mode == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            else:
                await asyncio.wait_for(task, 2)
            try:
                assert await asyncio.wait_for(reader.read(1), 0.4) == b""
            except ConnectionResetError:
                pass
            assert events == ["start", "stop"]
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()

    event_loop.run(scenario())
    assert {sig: signal.getsignal(sig) for sig in before} == before
