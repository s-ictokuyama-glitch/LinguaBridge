"""テスト用のPCM生成ヘルパー。

FakeASREngine の規約: 発話中の最初の非ゼロサンプル値がフレーズ辞書のキー
（1000/2000/3000）に一致すると既知の日本語文になる。
"""

from __future__ import annotations

from collections.abc import Coroutine, Iterator
from typing import TypeVar, overload

import numpy as np
from starlette.testclient import WebSocketTestSession

from server.config import AppConfig, VadConfig
from server.ws_protocol import ServerMessage, parse_server_message

JOIN_CODE = "4831"

SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # ブラウザが送る100msフレーム相当


def speech_pcm(key: int, seconds: float) -> np.ndarray:
    return np.full(int(SAMPLE_RATE * seconds), key, dtype=np.int16)


def silence_pcm(seconds: float) -> np.ndarray:
    return np.zeros(int(SAMPLE_RATE * seconds), dtype=np.int16)


def chunks(pcm: np.ndarray, chunk_bytes: int = CHUNK_BYTES) -> Iterator[bytes]:
    data = pcm.tobytes()
    for i in range(0, len(data), chunk_bytes):
        yield data[i : i + chunk_bytes]


def utterance_bytes(key: int, speech_s: float = 1.0, silence_s: float = 1.2) -> Iterator[bytes]:
    """発話1件分（音声＋発話終了判定に足る無音）を100msチャンク列で返す。

    無音は既定 turn 戦略（morph）の `force_silence_ms`(1000ms) を超える長さにしてある。
    PHRASES[3000]「今日は天気がいいですね。」は文法上「継続」に分類されるので、
    無音がここに届かないと Turn が確定せず字幕が出ない（#27）。
    """
    pcm = np.concatenate([speech_pcm(key, speech_s), silence_pcm(silence_s)])
    return chunks(pcm)


class RecordingSocket:
    def __init__(self) -> None:
        import asyncio
        self.sent: list[dict] = []
        self.closed_with: int | None = None
        self._frames: asyncio.Queue[dict] = asyncio.Queue()

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)
        self._frames.put_nowait(data)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code

    async def receive_json(self) -> dict:
        return await self._frames.get()


_Message = TypeVar("_Message", bound=ServerMessage)


@overload
def wait_for(socket: RecordingSocket, kind: str, **fields: object) -> Coroutine[None, None, dict]: ...


@overload
def wait_for(
    socket: WebSocketTestSession,
    kind: type[_Message],
    *,
    received: list[ServerMessage] | None = None,
    **fields: object,
) -> _Message: ...


def wait_for(
    socket: RecordingSocket | WebSocketTestSession,
    kind: str | type[_Message],
    **fields: object,
) -> Coroutine[None, None, dict] | _Message:
    if isinstance(socket, RecordingSocket):
        if not isinstance(kind, str):
            raise TypeError("RecordingSocket requires a string message type")
        return _wait_for_recording(socket, kind, **fields)
    if isinstance(kind, str):
        raise TypeError("WebSocketTestSession requires a server message model")
    received = fields.pop("received", None)
    if received is not None and not isinstance(received, list):
        raise TypeError("received must be a list of server messages")
    while True:
        message = parse_server_message(socket.receive_text())
        if received is not None:
            received.append(message)
        if isinstance(message, kind) and all(
            getattr(message, key) == value for key, value in fields.items()
        ):
            return message


async def _wait_for_recording(socket: RecordingSocket, kind: str, **fields: object) -> dict:
    import asyncio
    async with asyncio.timeout(5):
        while True:
            frame = await socket.receive_json()
            if frame.get("type") == kind and all(frame.get(k) == v for k, v in fields.items()):
                return frame


def make_ws_test_config() -> AppConfig:
    """WS境界テスト用の設定。VADは決定的な energy
    （テストが送る定数振幅PCMを Silero は音声と判定しないため）。"""
    return AppConfig(vad=VadConfig(engine="energy", threshold=300))
