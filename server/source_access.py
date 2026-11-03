"""接続元 IP に基づく HTTP と WebSocket 共通の入口。"""
from __future__ import annotations

import logging
from ipaddress import IPv4Address, IPv6Address, ip_address

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


def socket_ip(address: tuple[str, int] | None) -> IPv4Address | IPv6Address | None:
    if address is None:
        return None
    try:
        return ip_address(address[0])
    except ValueError:
        return None


class SourceAccess:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        ip = socket_ip(client)
        if ip is not None and not ip.is_global:
            await self.app(scope, receive, send)
            return

        logger.warning("送信元を拒否: %r (%s)", client, kind)
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            await JSONResponse({"detail": "forbidden"}, status_code=403)(scope, receive, send)
