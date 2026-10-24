"""起動時に確定する公開接続先と証明書の状態。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import threading
from urllib.parse import urlencode

from server import network
from server.certificates import certificate_ready, inspect_certificate
from server.config import ServerConfig


@dataclass(frozen=True, init=False)
class Publication:
    selected: network.InterfaceAddress
    http_port: int
    https_port: int
    cert_path: Path
    key_path: Path
    https_available: bool
    _tls: dict = field(repr=False, compare=False)
    _lock: threading.Lock = field(repr=False, compare=False)
    _error: str | None = field(default=None, repr=False, compare=False)

    def __init__(self, config: ServerConfig, *, requested_ip: str | None = None,
                 interactive: bool = False):
        selected = network.choose_address(
            requested_ip if requested_ip is not None else config.advertise_ip,
            interactive=interactive,
        )
        tls = inspect_certificate(config, selected.ip)
        for name, value in (
            ("selected", selected), ("http_port", config.http_port),
            ("https_port", config.https_port), ("cert_path", config.cert_path()),
            ("key_path", config.key_path()), ("_tls", deepcopy(tls)),
            ("https_available", certificate_ready(tls)), ("_error", None),
            ("_lock", threading.Lock()),
        ):
            object.__setattr__(self, name, value)

    @property
    def ip(self) -> str:
        return self.selected.ip

    @property
    def tls(self) -> dict:
        return deepcopy(self._tls)

    @staticmethod
    def loopback_url(port: int, path: str = "/") -> str:
        return f"http://127.0.0.1:{port}{path}"

    def http_url(self, path: str = "/", *, loopback: bool = False) -> str:
        return f"http://{'127.0.0.1' if loopback else self.ip}:{self.http_port}{path}"

    def https_url(self, path: str = "/") -> str:
        return f"https://{self.ip}:{self.https_port}{path}"

    @property
    def teacher_url(self) -> str:
        return (self.https_url("/teacher") if self.https_available else
                self.http_url("/teacher", loopback=True))

    def join_url(self, code: str) -> str:
        return self.http_url("/?" + urlencode({"code": code}))

    def current(self) -> Publication:
        with self._lock:
            if self._error is None:
                addresses = network.list_addresses()
                if not any(item.usable and (item.name, item.ip) ==
                           (self.selected.name, self.ip) for item in addresses):
                    object.__setattr__(self, "_error",
                        "ネットワークが変更されました。サーバーを再起動し、接続先を再選択してください。")
            if self._error is not None:
                raise ValueError(self._error)
        return self
