"""先生画面のAPI応答→QR表示→接続先消失時の消去をDOM境界で確認する。"""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_teacher_clears_qr_and_url_when_network_becomes_invalid():
    node = shutil.which("node")
    if node is None:
        pytest.skip("先生画面のJavaScript検証にはNode.jsが必要")
    script = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const elements = new Map();
const replies = [
  {ok: true, json: async () => ({code: "4831", join_url: "http://10.53.64.130:8000/?code=4831"})},
  {ok: false, status: 503, json: async () => ({detail: "接続先が消失しました。再起動してください。"})},
];
const sandbox = {
  document: {getElementById(id) {
    if (!elements.has(id)) elements.set(id, {textContent: "", hidden: true});
    return elements.get(id);
  }},
  fetch: async () => replies.shift(),
  AbortSignal,
  QRCode: function(element, options) { element.textContent = options.text; },
};
vm.createContext(sandbox);
// 起動イベントは呼ばず、画面が利用する情報更新入口を実行する。
vm.runInContext(fs.readFileSync("web/teacher.js", "utf8").replace(/init\(\);\s*$/, ""), sandbox);
(async () => {
  elements.get("page-error").textContent = "別の先生タブに接続を引き継ぎました。";
  elements.get("page-error").hidden = false;
  await sandbox.refreshJoinInfo();
  assert.equal(elements.get("page-error").hidden, false);
  assert.equal(elements.get("qr").textContent, "http://10.53.64.130:8000/?code=4831");
  assert.equal(elements.get("join-url").textContent, elements.get("qr").textContent);
  await sandbox.refreshJoinInfo();
  for (const id of ["qr", "join-url", "join-code"]) assert.equal(elements.get(id).textContent, "");
  assert.equal(elements.get("network-error").hidden, false);
  assert.match(elements.get("network-error").textContent, /再起動/);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
