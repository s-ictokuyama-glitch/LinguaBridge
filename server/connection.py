"""WebSocket 接続ごとの参加と入力の認可。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from server import ws_protocol as proto
from server.config import LimitsConfig
from server.lesson import Lesson
from server.roster import Client, ClientSocket

logger = logging.getLogger(__name__)


class Connection:
    def __init__(self, socket: ClientSocket, client_ip: str, lesson: Lesson,
                 limits: LimitsConfig) -> None:
        self._socket = socket
        self._client_ip = client_ip
        self._lesson = lesson
        self._limits = limits
        self._client: Client | None = None

    @contextlib.asynccontextmanager
    async def handling(self) -> AsyncIterator[None]:
        try:
            yield
        except Exception as error:
            await self._abort(error)
        finally:
            client = next(
                (member for member in self._lesson.roster.clients if member.ws is self._socket),
                self._client,
            )
            if client is not None:
                try:
                    await self._lesson.leave(client)
                except Exception as error:
                    await self._abort(error)

    async def _abort(self, error: Exception) -> None:
        logger.error("接続の処理で想定外のエラーが発生しました", exc_info=error)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._socket.close(code=1011), self._limits.send_timeout_s)

    def _current_client(self) -> Client | None:
        client = self._client
        return client if client is not None and self._lesson.roster.contains(client) else None

    @staticmethod
    def _is_teacher(client: Client | None) -> bool:
        return client is not None and client.role == "teacher"

    async def receive(self, frame: bytes | str) -> None:
        if isinstance(frame, bytes):
            if len(frame) > self._limits.max_audio_bytes:
                logger.warning("上限超過の音声フレームを破棄: %d bytes", len(frame))
                self._lesson.pipeline.reject_audio_frame()
            elif self._is_teacher(self._current_client()):
                await self._lesson.pipeline.feed_audio(frame)
            return

        if len(frame.encode("utf-8", "ignore")) > self._limits.max_text_bytes:
            await self._send_error("too_large", "メッセージが大きすぎます")
            return
        try:
            message = proto.parse_client_message(frame)
        except proto.ProtocolError as error:
            await self._send_error("bad_message", str(error))
            return
        if isinstance(message, proto.JoinMessage):
            self._client = await self._lesson.join(
                self._socket, self._client, message, self._client_ip
            )
            return

        client = self._current_client()
        if client is None:
            await self._send_error("not_joined", "先に join してください")
        elif isinstance(message, proto.SetLangMessage):
            await self._lesson.set_lang(client, message.lang)
        elif self._is_teacher(client):
            if isinstance(message, proto.ControlMessage):
                await self._lesson.control(message.action)
            elif isinstance(message, proto.RecordingMessage):
                self._lesson.set_recording(message.on)

    async def _send_error(self, code: proto.ErrorCode, message: str) -> None:
        await self._lesson.send_now(
            self._socket, proto.ErrorMsg(code=code, message=message).model_dump()
        )
