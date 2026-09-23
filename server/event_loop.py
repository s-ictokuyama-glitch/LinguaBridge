"""サーバーを動かすイベントループ（#42）。

Windows 既定の ProactorEventLoop は、accept 中の OSError を種類を問わず
「待受の故障」とみなし、例外ハンドラへ報告したうえで待受ソケットを閉じる
（CPython 3.12〜3.14 で同一実装。ランタイム更新では解消しない）。
接続確立前にクライアントが RST を送ると AcceptEx は WinError 64 等で失敗し、
その1件で HTTP/HTTPS の待受が以後すべての新規接続を拒否する（ローカルで再現）。

ここでは相手側リセット由来の失敗だけを接続1件の失敗として警告ログに残し、
accept を続ける。それ以外の OSError は既定どおり例外ハンドラへ報告して待受を閉じる。

戻し方: 環境変数 LINGUABRIDGE_STOCK_EVENT_LOOP=1 で再起動すると既定のループに戻る
（docs/connection-reset-2026-09-23.md）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Callable, Coroutine, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

STOCK_LOOP_ENV = "LINGUABRIDGE_STOCK_EVENT_LOOP"

# ERROR_NETNAME_DELETED / ERROR_CONNECTION_ABORTED / WSAECONNABORTED / WSAECONNRESET
PEER_RESET_WINERRORS = frozenset({64, 1236, 10053, 10054})


def is_peer_reset(exc: OSError) -> bool:
    """accept 失敗が、待受ではなく接続してきた相手1件の切断によるものか。"""
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
        return True
    return getattr(exc, "winerror", None) in PEER_RESET_WINERRORS


def loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """本番で使うループ。None は asyncio 既定（Windows 以外、または戻し指定時）。"""
    if sys.platform != "win32" or os.environ.get(STOCK_LOOP_ENV) == "1":
        return None
    return ResetTolerantProactorEventLoop


def run(main: Coroutine[Any, Any, T]) -> T:
    """asyncio.run と同じ。ただし loop_factory() のループで動かす。"""
    return asyncio.run(main, loop_factory=loop_factory())


if sys.platform == "win32":
    import socket
    import struct
    from asyncio import exceptions, tasks, trsock, windows_events

    _overlapped = windows_events._overlapped  # type: ignore[attr-defined]

    class ResetTolerantIocpProactor(windows_events.IocpProactor):
        # CPython 3.12 の IocpProactor.accept の写し。既定では accept 失敗時に
        # 受け側ソケット conn を閉じず、補助タスクが同じ例外を再送出して
        # 「Task exception was never retrieved」になる。相手側リセットの場合は
        # conn を閉じ、例外は accept ループ側（future の結果）だけで扱う。
        def accept(self, listener):
            self._register_with_iocp(listener)  # type: ignore[attr-defined]
            conn = self._get_accept_socket(listener.family)  # type: ignore[attr-defined]
            ov = _overlapped.Overlapped(windows_events.NULL)
            ov.AcceptEx(listener.fileno(), conn.fileno())

            def finish_accept(trans, key, ov):
                ov.getresult()
                # Use SO_UPDATE_ACCEPT_CONTEXT so getsockname() etc work.
                buf = struct.pack("@P", listener.fileno())
                conn.setsockopt(socket.SOL_SOCKET, _overlapped.SO_UPDATE_ACCEPT_CONTEXT, buf)
                conn.settimeout(listener.gettimeout())
                return conn, conn.getpeername()

            async def accept_coro(future, conn):
                try:
                    await future
                except exceptions.CancelledError:
                    conn.close()
                    raise
                except OSError as exc:
                    if not is_peer_reset(exc):
                        raise
                    conn.close()

            future = self._register(ov, listener, finish_accept)  # type: ignore[attr-defined]
            coro = accept_coro(future, conn)
            tasks.ensure_future(coro, loop=self._loop)  # type: ignore[attr-defined]
            return future

    class ResetTolerantProactorEventLoop(asyncio.ProactorEventLoop):
        def __init__(self, proactor=None):
            super().__init__(proactor or ResetTolerantIocpProactor())

        # CPython 3.12 の BaseProactorEventLoop._start_serving の写し。違いは、完了した
        # accept の結果が相手側リセットだった場合に警告を残して次の accept へ進むことだけ。
        # accept の発行自体の失敗など、それ以外は既定どおり待受を閉じる。
        def _start_serving(
            self, protocol_factory, sock, sslcontext=None, server=None, backlog=100,
            ssl_handshake_timeout=None, ssl_shutdown_timeout=None,
        ):
            def loop(f=None):
                try:
                    if f is not None:
                        try:
                            conn, addr = f.result()
                        except OSError as exc:
                            if sock.fileno() == -1 or not is_peer_reset(exc):
                                raise
                            logger.warning(
                                "接続確立前に相手側が切断したため、この1件を破棄して待受を継続: %r",
                                exc,
                            )
                        else:
                            if self._debug:  # type: ignore[attr-defined]
                                logger.debug(
                                    "%r got a new connection from %r: %r", server, addr, conn
                                )
                            protocol = protocol_factory()
                            if sslcontext is not None:
                                self._make_ssl_transport(  # type: ignore[attr-defined]
                                    conn, protocol, sslcontext, server_side=True,
                                    extra={"peername": addr}, server=server,
                                    ssl_handshake_timeout=ssl_handshake_timeout,
                                    ssl_shutdown_timeout=ssl_shutdown_timeout)
                            else:
                                self._make_socket_transport(  # type: ignore[attr-defined]
                                    conn, protocol, extra={"peername": addr}, server=server)
                    if self.is_closed():
                        return
                    f = self._proactor.accept(sock)  # type: ignore[attr-defined]
                except OSError as exc:
                    if sock.fileno() != -1:
                        self.call_exception_handler({
                            "message": "Accept failed on a socket",
                            "exception": exc,
                            "socket": trsock.TransportSocket(sock),
                        })
                        sock.close()
                    elif self._debug:  # type: ignore[attr-defined]
                        logger.debug("Accept failed on socket %r", sock, exc_info=True)
                except exceptions.CancelledError:
                    sock.close()
                else:
                    self._accept_futures[sock.fileno()] = f  # type: ignore[attr-defined]
                    f.add_done_callback(loop)

            self.call_soon(loop)
