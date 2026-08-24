"""クライアントごとの有界送信キュー（#25 A-3）。

以前は `broadcast` が全クライアントへ `await ws.send_json` を直列実行していた。
教室では電波の悪い端末が必ず1台は出る。その1接続のTCP輻輳が mt-worker を止め、
翻訳キューと他の生徒まで停滞させていた（head-of-line ブロッキング）。

ここでは1クライアントにつきキュー1本・送信タスク1本を持たせ、送信側を
非ブロッキングにする。遅い端末の遅れはその端末のキューに閉じ込められ、
溢れたらその接続だけを切る。生徒クライアントは自動再接続し、`last_seq` で
欠落分を差分復元するので、字幕そのものは失われない（web/student.js の close ハンドラ）。

キューとタスクが1本ずつなので、1クライアントへのメッセージ順序は投入順のまま保たれる。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from server.session import ClientSocket

logger = logging.getLogger(__name__)

# 溢れ・タイムアウトで切るときのWSクローズコード。1013 = Try Again Later。
# 生徒クライアントはこのコードでも通常の切断として再接続する
_OVERFLOW_CLOSE_CODE = 1013


class ClientSender:
    """1クライアントへの送信口。`send` は決してブロックしない。

    送信タスクが1本でキューから取り出して送る。溢れるか送信が
    `send_timeout_s` を超えたら、その接続を閉じて以後の送信を捨てる。
    """

    def __init__(
        self,
        ws: ClientSocket | None,
        *,
        maxsize: int,
        send_timeout_s: float,
        label: str = "",
    ) -> None:
        self._ws = ws
        self._send_timeout_s = send_timeout_s
        self._label = label
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=maxsize)
        self._alive = ws is not None  # ws=None（ユニットテスト）は最初から送らない
        self._task: asyncio.Task[None] | None = (
            asyncio.create_task(self._run(), name=f"sender-{label}") if ws is not None else None
        )
        self._close_task: asyncio.Task[None] | None = None

    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def send(self, payload: dict) -> bool:
        """送信を予約する。ブロックしない。False は「この接続へは届かない」。"""
        if not self._alive:
            return False
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            # 追いつけない端末。ここで待つと他の生徒まで巻き込むので接続を切る。
            # クライアントは再接続し、last_seq で欠落分を復元する
            logger.warning("送信キューが溢れたため切断します（client=%s）", self._label)
            self._kill()
            return False
        return True

    async def _run(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                await self._send_one(payload)
            finally:
                self._queue.task_done()
            if not self._alive:
                return

    async def _send_one(self, payload: dict) -> None:
        assert self._ws is not None
        try:
            await asyncio.wait_for(self._ws.send_json(payload), timeout=self._send_timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "送信が %.1f 秒で完了しないため切断します（client=%s）",
                self._send_timeout_s,
                self._label,
            )
            self._kill()
        except Exception:
            # 切断済み。クライアント除去はWSハンドラの finally が行う
            self._alive = False

    def _kill(self) -> None:
        """以後の送信を止め、WSを閉じる。close 自体も待たない（背後で走らせる）。"""
        if not self._alive:
            return
        self._alive = False
        ws = self._ws
        if ws is None:
            return

        async def _close() -> None:
            with contextlib.suppress(Exception):
                await ws.close(code=_OVERFLOW_CLOSE_CODE)

        # 呼び出し元（送信側・ワーカー）を待たせないため、切断は独立タスクにする。
        # 参照を持たないタスクはGCで消えうるので、aclose まで保持する
        self._close_task = asyncio.create_task(_close(), name=f"close-{self._label}")

    async def aclose(self, *, drain_timeout_s: float = 1.0) -> None:
        """送信タスクを畳む。残りは drain_timeout_s まで送り切ってから諦める。"""
        if self._close_task is not None:
            with contextlib.suppress(Exception):
                await self._close_task
            self._close_task = None
        if self._task is None:
            return
        if self._alive:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._queue.join(), timeout=drain_timeout_s)
        self._alive = False
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None
