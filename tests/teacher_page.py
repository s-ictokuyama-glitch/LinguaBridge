"""先生画面 web/teacher.js をNode.jsのvmで読み込み、ブラウザーの境界を差し替えて動かす。

QRライブラリは渡された文字列を要素へ書くだけの代役にする（QR画像の読取りは行わない）。
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

_HARNESS = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const elements = new Map();
const replies = __REPLIES__;
const sockets = [];
const sent = [];
const timers = [];
const sandbox = {
  document: {
    body: {dataset: {}},
    getElementById(id) {
      if (!elements.has(id)) {
        elements.set(id, Object.assign(new EventTarget(), {
          textContent: "", hidden: true, classList: {remove() {}},
        }));
      }
      return elements.get(id);
    },
  },
  location: {protocol: "https:", host: "teacher.test"},
  navigator: {},
  setTimeout(fn, delay) { timers.push({fn, delay}); },
  WebSocket: class extends EventTarget {
    constructor() {
      super();
      sockets.push(this);
    }
    send(data) { sent.push(JSON.parse(data)); }
    emit(message) {
      this.dispatchEvent(new MessageEvent("message", {data: JSON.stringify(message)}));
    }
    close() { this.dispatchEvent(new Event("close")); }
  },
  fetch: async () => {
    const reply = replies.shift();
    return {ok: reply.status === 200, status: reply.status, json: async () => reply.body};
  },
  AbortSignal,
  QRCode: function(element, options) { element.textContent = options.text; },
};
vm.createContext(sandbox);
// テストから必要な入口を呼ぶため、スクリプト末尾の自動起動を外す。
vm.runInContext(fs.readFileSync("web/teacher.js", "utf8").replace(/init\(\);\s*$/, ""), sandbox);
(async () => {
__BODY__
})().catch(error => { console.error(error); process.exitCode = 1; });
'''


def run_teacher_script(replies: list[dict], body: str) -> str:
    """`/api/teacher-info` へ順に replies（{"status", "body"}）を返し、body のJSを実行して標準出力を返す。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("先生画面のJavaScript検証にはNode.jsが必要")
    script = _HARNESS.replace("__REPLIES__", json.dumps(replies)).replace("__BODY__", body)
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True,
                            text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout
