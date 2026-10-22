"""クライアントごとの有界送信キュー（#25 A-3）のユニットテスト。

主張は3つ。
- `send` は決してブロックしない（遅い端末がワーカーを止めない）
- 溢れ・送信タイムアウトはその接続だけを切る（他の生徒に波及しない）
- 1クライアントへの順序は投入順のまま（字幕が入れ替わらない）
"""

from __future__ import annotations

import asyncio

import pytest

from server.delivery import ClientSender

TIMEOUT_S = 0.2


class FakeSocket:
    """送信に delay_s かかるソケット。close されたら記録する。"""

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.sent: list[dict] = []
        self.closed_with: int | None = None

    async def send_json(self, data: object) -> None:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        self.sent.append(dict(data))  # type: ignore[arg-type]

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


class BrokenSocket(FakeSocket):
    async def send_json(self, data: object) -> None:
        raise ConnectionResetError("切断済み")


async def _wait_until(predicate, timeout_s: float = 1.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def _make(ws, *, maxsize: int = 4, send_timeout_s: float = TIMEOUT_S) -> ClientSender:
    return ClientSender(ws, maxsize=maxsize, send_timeout_s=send_timeout_s, label="t")


class TestNonBlocking:
    def test_send_returns_immediately_even_for_a_stalled_socket(self):
        """1件の送信に何秒かかろうと、send の呼び出し自体は即座に返る。"""

        async def scenario() -> None:
            ws = FakeSocket(delay_s=5.0)
            sender = _make(ws)
            loop = asyncio.get_running_loop()
            started = loop.time()
            for i in range(4):
                assert sender.send({"seq": i}) is True
            assert loop.time() - started < 0.1
            await sender.aclose(drain_timeout_s=0.05)

        asyncio.run(scenario())

    def test_delivery_keeps_the_order_it_was_queued_in(self):
        async def scenario() -> None:
            ws = FakeSocket()
            sender = _make(ws, maxsize=16)
            for i in range(10):
                sender.send({"seq": i})
            await sender.aclose()
            assert [m["seq"] for m in ws.sent] == list(range(10))

        asyncio.run(scenario())


class TestIsolation:
    def test_overflow_closes_only_that_connection(self):
        """キューが溢れたら、その接続を切って以後の送信を捨てる。

        待つ設計にすると遅い端末1台が全員を止めるので、切る側を選んでいる
        （生徒クライアントは再接続して last_seq で欠落分を復元する）。
        """

        async def scenario() -> None:
            ws = FakeSocket(delay_s=5.0)
            sender = _make(ws, maxsize=2)
            results = [sender.send({"seq": i}) for i in range(6)]
            assert results[0] is True
            assert results[-1] is False, "上限を超えても受け付け続けている"
            assert sender.alive is False
            assert await _wait_until(lambda: ws.closed_with is not None)
            await sender.aclose(drain_timeout_s=0.05)

        asyncio.run(scenario())

    def test_send_timeout_closes_the_connection(self):
        async def scenario() -> None:
            ws = FakeSocket(delay_s=5.0)
            sender = _make(ws, maxsize=8, send_timeout_s=0.05)
            sender.send({"seq": 1})
            assert await _wait_until(lambda: not sender.alive)
            assert ws.sent == [], "タイムアウトしたのに送信済みになっている"
            await sender.aclose(drain_timeout_s=0.05)

        asyncio.run(scenario())

    def test_broken_socket_does_not_raise_into_the_caller(self):
        """切断済みの接続への送信は例外を呼び出し元へ漏らさない
        （クライアント除去はWSハンドラの finally が行う）。"""

        async def scenario() -> None:
            ws = BrokenSocket()
            sender = _make(ws)
            sender.send({"seq": 1})
            assert await _wait_until(lambda: not sender.alive)
            await sender.aclose(drain_timeout_s=0.05)

        asyncio.run(scenario())


class TestLifecycle:
    def test_aclose_drains_what_is_still_queued(self):
        async def scenario() -> None:
            ws = FakeSocket()
            sender = _make(ws, maxsize=8)
            for i in range(5):
                sender.send({"seq": i})
            await sender.aclose()
            assert len(ws.sent) == 5

        asyncio.run(scenario())

    def test_no_socket_means_no_task_and_no_send(self):
        """ユニットテストのダミークライアント（ws=None）では何も起こらない。"""

        async def scenario() -> None:
            sender = _make(None)
            assert sender.alive is False
            assert sender.send({"seq": 1}) is False
            await sender.aclose()

        asyncio.run(scenario())


@pytest.mark.parametrize("maxsize", [1, 4, 32])
def test_send_never_blocks_regardless_of_queue_size(maxsize: int):
    async def scenario() -> None:
        ws = FakeSocket(delay_s=5.0)
        sender = _make(ws, maxsize=maxsize)
        for i in range(maxsize * 3):
            sender.send({"seq": i})  # 溢れても例外にならず、待ちもしない
        await sender.aclose(drain_timeout_s=0.05)

    asyncio.run(scenario())
