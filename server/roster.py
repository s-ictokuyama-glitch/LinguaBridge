"""参加者と各接続の送信キューを所有する名簿。"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from server.config import LimitsConfig
from server.delivery import ClientSender

_REPLACED = 4000
_RETRY = 1013


class ClientSocket(Protocol):
    async def send_json(self, data: Any) -> None: ...
    async def close(self, code: int = 1000) -> None: ...


@dataclass
class Client:
    id: str
    role: Literal["teacher", "student"]
    lang: str | None
    ws: ClientSocket


class Roster:
    def __init__(self, limits: LimitsConfig | None = None) -> None:
        self._limits = limits or LimitsConfig()
        self._clients: dict[str, Client] = {}
        self._senders: dict[str, ClientSender] = {}

    @property
    def clients(self) -> tuple[Client, ...]:
        return tuple(self._clients.values())

    def get(self, client_id: str) -> Client | None:
        return self._clients.get(client_id)

    def contains(self, client: Client) -> bool:
        return self.get(client.id) is client

    def teacher(self) -> Client | None:
        return next((c for c in self._clients.values() if c.role == "teacher"), None)

    def students(self) -> list[Client]:
        return [c for c in self._clients.values() if c.role == "student"]

    def active_langs(self) -> set[str]:
        return {c.lang for c in self.students() if c.lang is not None}

    async def add(self, socket: ClientSocket, role: Literal["teacher", "student"],
                  lang: str | None = None, *, current: Client | None = None,
                  initial: dict | None = None) -> Client:
        old = self.teacher() if role == "teacher" else None
        client = Client(uuid.uuid4().hex, role, lang if role == "student" else None, socket)
        sender = self._detach(current) if current is not None and current.ws is socket else None
        if sender is None:
            sender = ClientSender(socket, maxsize=self._limits.send_queue_max,
                                  send_timeout_s=self._limits.send_timeout_s,
                                  label=f"{role}:{client.id[:8]}", close_code=_RETRY)
        self._clients[client.id] = client
        self._senders[client.id] = sender
        if initial is not None:
            sender.send(initial)
        retiring: list[tuple[Client, int | None]] = []
        if current is not None:
            retiring.append((current, None))
        if old is not None and old is not current:
            retiring.append((old, _REPLACED))
        detached = [(c, self._detach(c), code) for c, code in retiring]
        for retired, previous_sender, code in detached:
            if previous_sender is not None:
                await self._close(retired, previous_sender, code)
        return client

    def _detach(self, client: Client) -> ClientSender | None:
        if not self.contains(client):
            return None
        self._clients.pop(client.id)
        return self._senders.pop(client.id)

    async def _close(self, client: Client, sender: ClientSender,
                     close_code: int | None) -> None:
        await sender.aclose()
        if close_code is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.ws.close(code=close_code),
                                       timeout=self._limits.send_timeout_s)

    async def remove(self, client: Client, close_code: int | None = None) -> None:
        sender = self._detach(client)
        if sender is not None:
            await self._close(client, sender, close_code)

    async def retry(self, client: Client) -> None:
        await self.remove(client, close_code=_RETRY)

    def send(self, client: Client, payload: dict) -> None:
        sender = self._senders.get(client.id)
        if sender is not None:
            sender.send(payload)

    def send_teacher(self, payload: dict) -> None:
        teacher = self.teacher()
        if teacher is not None:
            self.send(teacher, payload)

    def send_language(self, lang: str, payload: dict) -> None:
        for client in self.students():
            if client.lang == lang:
                self.send(client, payload)

    def broadcast(self, payload: dict) -> None:
        for client in self.clients:
            self.send(client, payload)

    async def aclose(self) -> None:
        await asyncio.gather(*(self.remove(c) for c in self.clients))
