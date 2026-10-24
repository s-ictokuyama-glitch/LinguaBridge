"""ASGIアプリの複数リスナーと共有ライフサイクルを管理する。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
import signal
import socket
import threading
from typing import Any

import uvicorn
from uvicorn.lifespan.on import LifespanOn

GRACEFUL_SHUTDOWN_S = 10


@dataclass(frozen=True)
class TLSFiles:
    cert: Path
    key: Path


@dataclass(frozen=True)
class ListenerSpec:
    host: str
    port: int
    tls: TLSFiles | None = None
    lifespan: bool = False


class _Server(uvicorn.Server):
    def capture_signals(self):
        return nullcontext()

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        for server in self.servers:
            server.close()
        for sock in sockets or []:
            sock.close()
        for connection in list(self.server_state.connections):
            connection.shutdown()
        try:
            async with asyncio.timeout(GRACEFUL_SHUTDOWN_S):
                await self._wait_tasks_to_complete()
        except TimeoutError:
            for connection in list(self.server_state.connections):
                connection.transport.abort()
            tasks = list(self.server_state.tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class RunningListeners:
    def __init__(self, ports: tuple[int, ...], servers: list[_Server]):
        self.ports = ports
        self._servers = servers
        self._task: asyncio.Task[None] | None = None

    def stop(self) -> None:
        for server in self._servers:
            server.should_exit = True

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)


@asynccontextmanager
async def start_listeners(
    app: Any, specs: Sequence[ListenerSpec], *, install_signals: bool = True,
    on_ready: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[RunningListeners]:
    if not specs:
        raise ValueError("少なくとも1つのリスナーが必要です。")
    owners = [spec for spec in specs if spec.lifespan]
    if len(owners) > 1:
        raise ValueError("lifespanを持てるリスナーは1つです。")
    sockets: list[socket.socket] = []
    servers: list[_Server] = []
    tasks: list[asyncio.Task[None]] = []
    callback: asyncio.Task[None] | None = None
    handlers: dict[Any, Any] = {}
    lifespan: LifespanOn | None = None
    running = RunningListeners((), servers)
    try:
        for spec in specs:
            sock = socket.create_server((spec.host, spec.port))
            sock.setblocking(False)
            sockets.append(sock)
            config = uvicorn.Config(
                app, host=spec.host, port=spec.port, lifespan="off",
                log_config=None, access_log=False,
                ssl_certfile=str(spec.tls.cert) if spec.tls else None,
                ssl_keyfile=str(spec.tls.key) if spec.tls else None,
                timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
            )
            config.load()
            servers.append(_Server(config))
        if install_signals and threading.current_thread() is threading.main_thread():
            def stop(*_: object) -> None:
                running.stop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                handlers[sig] = signal.signal(sig, stop)
        if owners:
            lifespan_config = uvicorn.Config(app, lifespan="on", log_config=None)
            lifespan_config.load()
            lifespan = LifespanOn(lifespan_config)
            await lifespan.startup()
            if lifespan.should_exit:
                raise RuntimeError("アプリの起動処理が失敗しました。")
            # Uvicorn makes a shallow copy for each request, as ASGI specifies.
            for server in servers:
                original_app = server.config.loaded_app

                async def with_state(scope, receive, send, wrapped=original_app):
                    if scope["type"] != "lifespan":
                        scope["state"] = lifespan.state.copy()
                    await wrapped(scope, receive, send)

                server.config.loaded_app = with_state
        running.ports = tuple(sock.getsockname()[1] for sock in sockets)
        tasks = [asyncio.create_task(server.serve(sockets=[sock]))
                 for server, sock in zip(servers, sockets)]
        while not all(server.started for server in servers):
            for task in tasks:
                if task.done():
                    await task
                    raise RuntimeError("リスナーが起動前に停止しました。")
            await asyncio.sleep(0.01)
        if on_ready is not None:
            async def run_callback() -> None:
                assert on_ready is not None
                await on_ready()
            callback = asyncio.create_task(run_callback())

        async def supervise() -> None:
            pending = set(tasks)
            if callback is not None:
                pending.add(callback)
            try:
                while pending:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        await task
                        if task in tasks:
                            running.stop()
                    if not any(task in pending for task in tasks):
                        break
            finally:
                running.stop()
                if callback is not None:
                    callback.cancel()
                    await asyncio.gather(callback, return_exceptions=True)
                await asyncio.gather(*tasks, return_exceptions=True)

        running._task = asyncio.create_task(supervise())
        yield running
    finally:
        running.stop()
        if callback is not None:
            callback.cancel()
            await asyncio.gather(callback, return_exceptions=True)
        try:
            if running._task is not None:
                await running.wait()
            else:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for server in servers:
                for listener in getattr(server, "servers", []):
                    listener.close()
            for sock in sockets:
                sock.close()
            try:
                if lifespan is not None:
                    await lifespan.shutdown()
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
