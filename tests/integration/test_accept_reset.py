"""#42: Windows Proactor の accept 中に相手側がリセットしても待受を閉じない。

CPython 3.12〜3.14 は accept の OSError を種類を問わず待受の故障として扱い、
待受ソケットを閉じる。ローカルでは、接続→送信→即RSTのクライアントで
WinError 64 が起き、HTTP 待受が以後すべての新規接続を拒否した。
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import inspect
import logging
import socket
import sys
import textwrap

import pytest

from server import event_loop

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Proactor only")

# The error observed locally: OSError(22, ..., None, 64) = ERROR_NETNAME_DELETED.
NETNAME_DELETED = 64


def failing_first_accept(loop, exc, *, on_issue=False):
    """The next accept() fails once with exc, as AcceptEx does after a peer RST.

    on_issue=True raises while issuing the accept instead of in its result.
    """
    real_accept = loop._proactor.accept
    pending = [exc]

    def accept(listener):
        if pending and on_issue:
            raise pending.pop()
        if pending:
            future = loop.create_future()
            future.set_exception(pending.pop())
            return future
        return real_accept(listener)

    loop._proactor.accept = accept


async def serve_after_accept_error(exc, *, on_issue=False):
    """Start a loopback server whose first accept fails; report what happens next."""
    loop = asyncio.get_running_loop()
    handled = []
    loop.set_exception_handler(lambda _loop, context: handled.append(context))
    failing_first_accept(loop, exc, on_issue=on_issue)

    async def echo(reader, writer):
        writer.write(await reader.readexactly(4))
        await writer.drain()
        writer.close()

    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]  # known even if the listener is closed at once
    server = await asyncio.start_server(echo, sock=listener)
    await asyncio.sleep(0.05)  # let the failed accept be processed
    try:
        async with asyncio.timeout(3):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"ping")
            echoed = await reader.readexactly(4)
            writer.close()
            await writer.wait_closed()
    except ConnectionRefusedError as refused:  # 待受が閉じた（停止ではなく拒否）
        echoed = refused
    server.close()
    await server.wait_closed()
    return type(loop), echoed, handled


def test_peer_reset_during_accept_keeps_listener_serving(caplog):
    reset = OSError(errno.EINVAL, "指定されたネットワーク名は利用できません。", None, NETNAME_DELETED)
    with caplog.at_level(logging.WARNING, logger="server.event_loop"):
        loop_type, echoed, handled = event_loop.run(serve_after_accept_error(reset))

    assert loop_type is not asyncio.ProactorEventLoop  # production uses the guarded loop
    assert echoed == b"ping", "listener must keep accepting new clients"
    assert not handled, "a per-connection reset is not a listener failure"
    # Still observable: logged with its WinError, not silently swallowed.
    assert any("64" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    "reset",
    [ConnectionResetError(errno.ECONNRESET, "reset", None, 10054),
     ConnectionAbortedError(errno.ECONNABORTED, "aborted", None, 1236)],
    ids=["WSAECONNRESET", "ERROR_CONNECTION_ABORTED"],
)
def test_other_peer_reset_codes_keep_listener_serving(reset):
    _, echoed, handled = event_loop.run(serve_after_accept_error(reset))
    assert echoed == b"ping"
    assert not handled


def test_other_accept_errors_stay_visible_and_unchanged():
    # Not a peer reset (e.g. out of socket handles): keep the stock behaviour so
    # the operator sees it through the loop's exception handler.
    exhausted = OSError(errno.EMFILE, "too many open sockets", None, 10024)
    _, echoed, handled = event_loop.run(serve_after_accept_error(exhausted))

    assert isinstance(echoed, ConnectionRefusedError), "stock behaviour closes the listener"
    assert [c["message"] for c in handled] == ["Accept failed on a socket"]
    assert handled[0]["exception"] is exhausted


def test_reset_while_issuing_accept_is_not_retried():
    # Only a completed accept's result is treated as one lost client. A failure
    # to issue the accept keeps the stock handling, so it can never spin.
    reset = ConnectionResetError(errno.ECONNRESET, "reset", None, 10054)
    _, echoed, handled = event_loop.run(serve_after_accept_error(reset, on_issue=True))

    assert isinstance(echoed, ConnectionRefusedError)
    assert [c["message"] for c in handled] == ["Accept failed on a socket"]


def test_rollback_switch_restores_stock_loop(monkeypatch):
    # Restore procedure (docs/connection-reset-2026-09-23.md): set the variable
    # and restart. The stock loop is back, and with it the original defect.
    monkeypatch.setenv(event_loop.STOCK_LOOP_ENV, "1")
    reset = OSError(errno.EINVAL, "netname deleted", None, NETNAME_DELETED)
    loop_type, echoed, handled = event_loop.run(serve_after_accept_error(reset))

    assert loop_type is asyncio.ProactorEventLoop
    assert isinstance(echoed, ConnectionRefusedError)
    assert [c["message"] for c in handled] == ["Accept failed on a socket"]


# 写し元（CPython 3.12.10 と 3.14.2 で同一）のソース。ランタイム更新で変わったら
# server/event_loop.py の写しを見直し、ここを更新する。
STDLIB_COPIES = {
    "BaseProactorEventLoop._start_serving":
        "a4aac1923735b3a537cb29e586ae1236afc9dd45d9c7146e2442b25b739ca29e",
    "IocpProactor.accept":
        "81d7cd26c755de213d1701e6dabb54ad4c43d3a4c3d74ade95b4bcb24302553f",
}


def test_stdlib_originals_unchanged():
    from asyncio import proactor_events, windows_events

    originals = (proactor_events.BaseProactorEventLoop._start_serving,
                 windows_events.IocpProactor.accept)
    digests = {
        f.__qualname__: hashlib.sha256(textwrap.dedent(inspect.getsource(f)).encode()).hexdigest()
        for f in originals
    }
    assert digests == STDLIB_COPIES, "asyncio changed: re-review server/event_loop.py copies"
