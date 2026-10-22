"""#41: 先生・生徒ページは、開いたページと同じホスト・ポートへWS接続する。

サーバー側のOrigin防御（test_live_delivery.py）は、ページが自身のOriginで同じ待受へ
つなぐことを前提にしている。web/*.js をNode.jsのvmで読み込み、`connect` が作る
WebSocket のURLだけを観測する（DOM・ストレージは何もしない代役）。
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests.teacher_page import ROOT

_HARNESS = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const stub = new Proxy(function () {}, {
  get: (_, key) => key === Symbol.toPrimitive ? () => "" : stub,
  set: () => true,
  apply: () => stub,
});
const opened = [];
const sandbox = {
  document: stub, localStorage: stub, navigator: stub, window: stub,
  location: __LOCATION__,
  WebSocket: class { constructor(url) { opened.push(url); } addEventListener() {} },
  setTimeout, clearTimeout, setInterval, clearInterval, URLSearchParams, console,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(__FILE__, "utf8").replace(/init\(\);\s*$/, ""), sandbox);
vm.runInContext(__CALL__, sandbox);
console.log(JSON.stringify(opened));
'''


def opened_urls(page: str, call: str, protocol: str, host: str) -> list[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("ページのJavaScript検証にはNode.jsが必要")
    script = (_HARNESS
              .replace("__LOCATION__", json.dumps({
                  "protocol": protocol, "host": host, "hostname": host.rsplit(":", 1)[0],
                  "port": host.rsplit(":", 1)[1], "search": ""}))
              .replace("__FILE__", json.dumps(f"web/{page}"))
              .replace("__CALL__", json.dumps(call)))
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True,
                            text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# 先生は参加コード、生徒は再接続時の last_seq（初回は null）を渡して接続する
@pytest.mark.parametrize("page, call", [("teacher.js", 'connect("4831")'),
                                        ("student.js", "connect(null)")])
@pytest.mark.parametrize(
    "protocol, host, expected",
    [
        ("https:", "10.53.64.130:8443", "wss://10.53.64.130:8443/ws"),  # 先生ページの運用
        ("http:", "10.53.64.130:8000", "ws://10.53.64.130:8000/ws"),  # 生徒ページの運用
        ("http:", "127.0.0.1:8000", "ws://127.0.0.1:8000/ws"),  # サーバーPCで開いた先生ページ
    ],
)
def test_page_connects_to_its_own_host_and_port(page, call, protocol, host, expected):
    assert opened_urls(page, call, protocol, host) == [expected]
