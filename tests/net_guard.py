"""外部通信ゼロを機械判定するためのソケットガード（#23）。

「完全ローカル・クラウド送信ゼロ」はこのプロジェクトの絶対要件だが、
コードレビューでしか守られていなかった。ここでソケットの発信口を包み、
ループバック・LAN 以外への接続が1件でも起きたらテストを落とす。

包む対象は「宛先が決まる瞬間」だけ:
    socket.socket.connect / connect_ex / sendto / socket.create_connection

listen/accept（サーバー側の受け口）は包まない。LinguaBridge は LAN 内で
待ち受けるのが仕事なので、受信は違反ではない。

Windows の asyncio は自己パイプに loopback の socketpair を使うため、
ループバックを許可しないと全テストが落ちる（これは違反ではない）。
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
from collections.abc import Iterator
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Destination:
    """接続が試みられた宛先。"""

    raw: str  # ログに出す元の表現（AF_UNIX のパス等も入る）
    host: str | None  # 数値IPとして解釈できた場合のみ

    @property
    def is_local(self) -> bool:
        """ループバックまたはLAN内か。判定不能なもの（AF_UNIX等）はローカル扱い。"""
        if self.host is None:
            return True  # AF_UNIX / ソケットペア等。ネットワークに出ない
        try:
            addr = ipaddress.ip_address(self.host)
        except ValueError:
            return False  # 数値でないホスト名 = 名前解決を伴う外部接続の疑い
        return bool(
            addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_unspecified
        )


def _describe(address: object) -> Destination:
    if isinstance(address, (str, bytes)):
        return Destination(raw=str(address), host=None)  # AF_UNIX のパス
    if isinstance(address, tuple) and address:
        host = address[0]
        if isinstance(host, (bytes, bytearray)):
            host = host.decode("ascii", "replace")
        if isinstance(host, str):
            # IPv6 のスコープ付き表記（fe80::1%eth0）はスコープを落として判定する
            return Destination(raw=str(address), host=host.split("%", 1)[0])
        return Destination(raw=str(address), host=None)
    return Destination(raw=repr(address), host=None)


@dataclass
class NetGuard:
    """観測した宛先を貯める。テストは external が空であることを主張する。"""

    seen: list[Destination] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)

    @property
    def external(self) -> list[Destination]:
        return [d for d in self.seen if not d.is_local]

    def assert_no_external_traffic(self) -> None:
        if self.external:
            targets = ", ".join(sorted({d.raw for d in self.external}))
            raise AssertionError(
                f"ループバック・LAN 以外への接続が {len(self.external)} 件ありました: {targets}"
            )

    @property
    def external_lookups(self) -> list[str]:
        """FQDN らしき名前解決。自ホスト名（ドットなし）は除く。"""
        return [name for name in self.resolved if "." in name and name != "localhost"]


@contextlib.contextmanager
def guard_network() -> Iterator[NetGuard]:
    guard = NetGuard()

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, address):  # type: ignore[no-untyped-def]
        guard.seen.append(_describe(address))
        return real_connect(self, address)

    def connect_ex(self, address):  # type: ignore[no-untyped-def]
        guard.seen.append(_describe(address))
        return real_connect_ex(self, address)

    def sendto(self, *args):  # type: ignore[no-untyped-def]
        # sendto(data, address) と sendto(data, flags, address) の両形
        if args:
            guard.seen.append(_describe(args[-1]))
        return real_sendto(self, *args)

    def create_connection(address, *args, **kwargs):  # type: ignore[no-untyped-def]
        guard.seen.append(_describe(address))
        return real_create_connection(address, *args, **kwargs)

    def getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(host, str):
            guard.resolved.append(host)
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = sendto  # type: ignore[method-assign]
    socket.create_connection = create_connection  # type: ignore[assignment]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    try:
        yield guard
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        socket.socket.sendto = real_sendto  # type: ignore[method-assign]
        socket.create_connection = real_create_connection  # type: ignore[assignment]
        socket.getaddrinfo = real_getaddrinfo  # type: ignore[assignment]
