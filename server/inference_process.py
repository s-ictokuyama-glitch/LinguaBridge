from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
import math
import multiprocessing
from multiprocessing.process import BaseProcess
import pickle
import signal
import socket
import struct
from time import monotonic
from typing import TypeVar, cast

from server.inference import InferenceFailed, InferenceSucceeded, InferenceTimedOut, finish_cleanup
from server.model_files import TranslationModelError

T = TypeVar("T")
_METHODS = {"warmup", "transcribe", "translate"}
_HEADER = struct.Struct("!Q")


@dataclass(frozen=True)
class _Failure:
    kind: str
    message: str

    @classmethod
    def from_error(cls, error: Exception) -> _Failure:
        kind = "translation_model" if isinstance(error, TranslationModelError) else type(error).__name__
        return cls(kind, str(error))

    def exception(self) -> Exception:
        if self.kind == "translation_model":
            return TranslationModelError(self.message)
        if self.kind == "TimeoutError":
            return TimeoutError(self.message)
        return RuntimeError(f"{self.kind}: {self.message}")


@dataclass(frozen=True)
class _Reply:
    request_id: int
    value: object = None
    error: _Failure | None = None


def _frame(value: object) -> bytes:
    payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    return _HEADER.pack(len(payload)) + payload


def _read(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise EOFError("inference connection closed")
        data.extend(chunk)
    return bytes(data)


def _serve(factory: Callable[[], object], connection: socket.socket) -> None:
    # Ctrl+C は親が扱い、親の停止処理からこのプロセスを回収する。
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    connection.setblocking(True)
    try:
        try:
            engine = factory()
            getattr(engine, "warmup")()
        except Exception as error:
            connection.sendall(_frame(_Reply(0, error=_Failure.from_error(error))))
            return
        connection.sendall(_frame(_Reply(0)))
        while True:
            size, = _HEADER.unpack(_read(connection, _HEADER.size))
            request_id, method, args = pickle.loads(_read(connection, size))
            try:
                if method not in _METHODS:
                    raise ValueError("unsupported inference operation")
                value = getattr(engine, method)(*args)
                reply = _Reply(request_id, value=value)
                encoded = _frame(reply)
            except Exception as error:
                encoded = _frame(_Reply(request_id, error=_Failure.from_error(error)))
            connection.sendall(encoded)
    except (EOFError, OSError):
        pass
    finally:
        connection.close()


async def _receive(connection: socket.socket) -> _Reply:
    loop = asyncio.get_running_loop()

    async def read(size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = await loop.sock_recv(connection, size - len(data))
            if not chunk:
                raise EOFError("inference process exited without a response")
            data.extend(chunk)
        return bytes(data)

    size, = _HEADER.unpack(await read(_HEADER.size))
    # 接続はこの親から spawn した子だけに渡す。ネットワーク入力には公開しない。
    reply = pickle.loads(await read(size))
    if not isinstance(reply, _Reply):
        raise RuntimeError("invalid inference response")
    return reply


@dataclass
class _Generation:
    process: BaseProcess
    connection: socket.socket
    startup: asyncio.Task[InferenceFailed | None] | None = None
    request: asyncio.Task[_Reply] | None = None
    cleanup: asyncio.Task[None] | None = None


class ProcessInferenceRunner:
    def __init__(
        self, engine: object, factory: Callable[[], object], *, startup_timeout_s: float, name: str = ""
    ) -> None:
        if not math.isfinite(startup_timeout_s) or startup_timeout_s <= 0:
            raise ValueError("startup_timeout_s must be positive and finite")
        self._engine = engine
        self._factory = factory
        self._startup_timeout_s = startup_timeout_s
        self._name = name
        self._gate = asyncio.Lock()
        self._generation: _Generation | None = None
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._request_id = 0

    def _operation(self, operation: Callable[[], T]) -> tuple[str, tuple]:
        args = ()
        if isinstance(operation, partial):
            if operation.keywords:
                raise ValueError("inference operations require positional arguments")
            args, operation = operation.args, operation.func
        method = getattr(operation, "__name__", "")
        if (method not in _METHODS or getattr(operation, "__self__", None) is not self._engine
                or getattr(operation, "__func__", None) is not getattr(type(self._engine), method, None)):
            raise ValueError("operation must be an allowed method of this engine")
        if method == "warmup" and args:
            raise ValueError("warmup takes no arguments")
        return method, args

    async def _prepare(self, generation: _Generation, deadline: float) -> InferenceFailed | None:
        try:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("inference startup deadline expired")
            async with asyncio.timeout_at(deadline):
                reply = await _receive(generation.connection)
            if reply.request_id != 0:
                raise RuntimeError("unexpected inference startup response")
            if reply.error is not None:
                raise reply.error.exception()
            return None
        except Exception as error:
            self._retire(generation)
            return InferenceFailed(error)

    async def _get_generation(self) -> _Generation:
        if self._generation is not None and self._generation.cleanup is not None:
            await asyncio.shield(self._generation.cleanup)
            self._generation = None
        if self._closed:
            raise RuntimeError("inference runner stopped")
        if self._generation is None:
            # Windows は子を生成してから factory を書き込むため、直列化失敗は生成前に検出する。
            pickle.dumps(self._factory, protocol=pickle.HIGHEST_PROTOCOL)
            parent, child = socket.socketpair()
            parent.setblocking(False)
            process = multiprocessing.get_context("spawn").Process(
                target=_serve, args=(self._factory, child), name=f"inference-{self._name}", daemon=True,
            )
            generation = self._generation = _Generation(process, parent)
            try:
                deadline = asyncio.get_running_loop().time() + self._startup_timeout_s
                process.start()
                generation.startup = asyncio.create_task(self._prepare(generation, deadline))
            except BaseException:
                await finish_cleanup(self._retire(generation))
                raise
            finally:
                child.close()
        return self._generation

    def _retire(self, generation: _Generation) -> asyncio.Task[None]:
        if generation.cleanup is None:
            generation.cleanup = asyncio.create_task(self._dispose(generation))
        return generation.cleanup

    async def _dispose(self, generation: _Generation) -> None:
        pending = [task for task in (generation.startup, generation.request) if task is not None]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        generation.connection.close()
        process = generation.process
        if process.pid is not None:
            if process.is_alive():
                process.kill()
            while process.is_alive():
                await asyncio.sleep(0.01)
            process.join()
        process.close()

    async def _exchange(self, generation: _Generation, request: bytes, request_id: int) -> _Reply:
        await asyncio.get_running_loop().sock_sendall(generation.connection, request)
        reply = await _receive(generation.connection)
        if reply.request_id != request_id:
            raise RuntimeError("inference response belongs to another request")
        return reply

    async def run(
        self, operation: Callable[[], T], *, timeout: float | None
    ) -> InferenceSucceeded[T] | InferenceTimedOut | InferenceFailed:
        started = monotonic()
        active: _Generation | None = None
        deadline = asyncio.timeout(timeout)
        try:
            method, args = self._operation(operation)
            async with deadline:
                async with self._gate:
                    generation = await self._get_generation()
                    assert generation.startup is not None
                    failure = await asyncio.shield(generation.startup)
                    if self._closed:
                        raise RuntimeError("inference runner stopped")
                    if failure is not None:
                        await finish_cleanup(self._retire(generation))
                        return failure
                    if method == "warmup":
                        return InferenceSucceeded(cast(T, None), int((monotonic() - started) * 1000))
                    self._request_id += 1
                    request_id = self._request_id
                    encoded = _frame((request_id, method, args))
                    active = generation
                    generation.request = asyncio.create_task(self._exchange(generation, encoded, request_id))
                    reply = await asyncio.shield(generation.request)
                    if reply.error is not None:
                        return InferenceFailed(reply.error.exception())
                    return InferenceSucceeded(cast(T, reply.value), int((monotonic() - started) * 1000))
        except TimeoutError as error:
            if active is not None:
                await finish_cleanup(self._retire(active))
            return InferenceTimedOut() if deadline.expired() else InferenceFailed(error)
        except asyncio.CancelledError:
            if active is not None:
                await finish_cleanup(self._retire(active))
            caller = asyncio.current_task()
            if self._closed and caller is not None and not caller.cancelling():
                return InferenceFailed(RuntimeError("inference runner stopped"))
            raise
        except Exception as error:
            if active is not None:
                await finish_cleanup(self._retire(active))
            return InferenceFailed(error)

    async def aclose(self) -> None:
        self._closed = True
        if self._close_task is None:
            if self._generation is None:
                return
            self._close_task = self._retire(self._generation)
        await finish_cleanup(self._close_task)
