"""server.config の参加URL生成（QRの中身）。

最近のブラウザ/QRリーダーは http:// を https:// に自動アップグレードすることがあるため、
ホスト（mDNS名など）とスキームを設定で差し替えられる。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.config import ServerConfig


def test_join_url_defaults_to_detected_lan_ip_over_http() -> None:
    cfg = ServerConfig()
    assert cfg.join_url("1234", "192.168.1.5") == "http://192.168.1.5:8000/?code=1234"


def test_join_url_uses_public_host_when_set() -> None:
    cfg = ServerConfig(public_host=" linguabridge.local ")
    # 前後の空白は落として使う（YAMLに書き写す際の事故を吸収）
    assert cfg.join_url("1234", "192.168.1.5") == "http://linguabridge.local:8000/?code=1234"


def test_join_url_https_scheme_uses_https_port() -> None:
    cfg = ServerConfig(join_scheme="https")
    assert cfg.join_url("9876", "192.168.1.5") == "https://192.168.1.5:8443/?code=9876"


def test_join_scheme_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        ServerConfig(join_scheme="ws")
