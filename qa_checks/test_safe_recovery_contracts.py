"""SAFE-02 / SAFE-04 の障害隔離と接続回復契約を検証する。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from starlette.testclient import TestClient

from server.asr.fake_engine import FakeASREngine
from server.delivery import ClientSender
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.integration.test_ws_boundary import join_teacher


class _Socket:
    def __init__(self, *, blocked: bool = False) -> None:
        self.blocked = blocked
        self.send_started = asyncio.Event()
        self.release = asyncio.Event()
        self.sent: list[dict] = []
        self.close_codes: list[int] = []

    async def send_json(self, data: object) -> None:
        self.send_started.set()
        if self.blocked:
            await self.release.wait()
        self.sent.append(dict(data))  # type: ignore[arg-type]

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)


async def _wait_until(predicate: Callable[[], bool], timeout_s: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("条件成立を待ってタイムアウトしました")
        await asyncio.sleep(0.005)


def test_overflow_closes_only_slow_sender_with_1013() -> None:
    async def scenario() -> None:
        slow_ws = _Socket(blocked=True)
        fast_ws = _Socket()
        slow = ClientSender(slow_ws, maxsize=1, send_timeout_s=5.0, label="slow")
        fast = ClientSender(fast_ws, maxsize=8, send_timeout_s=1.0, label="fast")
        payloads = [{"seq": seq} for seq in range(1, 4)]
        try:
            assert slow.send(payloads[0]) is True
            assert fast.send(payloads[0]) is True
            await asyncio.wait_for(slow_ws.send_started.wait(), timeout=1.0)

            assert slow.send(payloads[1]) is True
            assert fast.send(payloads[1]) is True
            assert slow.send(payloads[2]) is False
            assert fast.send(payloads[2]) is True

            await _wait_until(lambda: slow_ws.close_codes == [1013])
            await _wait_until(lambda: fast_ws.sent == payloads)
            assert slow.alive is False
            assert fast.alive is True
            assert fast_ws.close_codes == []
        finally:
            slow_ws.release.set()
            await slow.aclose(drain_timeout_s=0.05)
            await fast.aclose(drain_timeout_s=0.05)

    asyncio.run(scenario())


def test_timeout_closes_only_slow_sender_with_1013() -> None:
    async def scenario() -> None:
        slow_ws = _Socket(blocked=True)
        fast_ws = _Socket()
        slow = ClientSender(slow_ws, maxsize=8, send_timeout_s=0.03, label="slow")
        fast = ClientSender(fast_ws, maxsize=8, send_timeout_s=1.0, label="fast")
        payload = {"seq": 1}
        try:
            assert slow.send(payload) is True
            assert fast.send(payload) is True

            await _wait_until(lambda: slow_ws.close_codes == [1013])
            await _wait_until(lambda: fast_ws.sent == [payload])
            assert slow.alive is False
            assert fast.alive is True
            assert fast_ws.close_codes == []
        finally:
            slow_ws.release.set()
            await slow.aclose(drain_timeout_s=0.05)
            await fast.aclose(drain_timeout_s=0.05)

    asyncio.run(scenario())


def test_invalid_messages_do_not_poison_the_same_websocket() -> None:
    app = create_app(
        make_ws_test_config(),
        asr_engine=FakeASREngine(),
        mt_engine=FakeTranslationEngine(["en", "zh"]),
        join_code=JOIN_CODE,
    )

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            invalid_messages = [
                ("{not-json", "bad_message"),
                ('{"type":"unknown_operation"}', "bad_message"),
                ('{"type":"control","action":"start"}', "not_joined"),
            ]
            for raw, expected_code in invalid_messages:
                ws.send_text(raw)
                response = ws.receive_json()
                assert response["type"] == "error"
                assert response["code"] == expected_code

            assert join_teacher(ws)["type"] == "joined"
            ws.send_json({"type": "control", "action": "start"})
            assert ws.receive_json() == {"type": "session", "state": "live"}
