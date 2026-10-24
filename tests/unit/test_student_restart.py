import shutil
import subprocess

import pytest

from tests.teacher_page import ROOT


@pytest.mark.parametrize("resume", ['{type: "session", state: "live"}',
                                     '{type: "joined", session_state: "live", history_from: 1}'])
def test_student_reconnects_with_watermark_after_lesson_restart(resume):
    node = shutil.which("node")
    if node is None:
        pytest.skip("ページのJavaScript検証にはNode.jsが必要")
    script = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const stub = new Proxy(function () {}, {
  get: (_, key) => key === Symbol.toPrimitive ? () => "" : stub,
  set: () => true,
  apply: () => stub,
});
const sockets = [];
const timers = [];
const sandbox = {
  document: stub, localStorage: stub, navigator: stub, window: stub,
  I18N: {ja: {}}, location: {protocol: "http:", host: "localhost:8000"},
  WebSocket: class {
    constructor() { this.events = {}; this.sent = []; sockets.push(this); }
    addEventListener(kind, callback) { this.events[kind] = callback; }
    send(value) { this.sent.push(JSON.parse(value)); }
  },
  setTimeout: callback => timers.push(callback), URLSearchParams, console,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync("web/student.js", "utf8").replace(/init\(\);\s*$/, ""), sandbox);
vm.runInContext(`
  connect(null);
  handleMessage({type: "joined", session_state: "ended", history_from: 1});
`, sandbox);
sockets[0].events.close();
assert.equal(timers.length, 0);
vm.runInContext(`
  handleMessage(__RESUME__);
  handleMessage({type: "caption", seq: 1});
`, sandbox);
sockets[0].events.close();
assert.equal(timers.length, 1);
timers.shift()();
sockets[1].events.open();
assert.equal(sockets[1].sent[0].last_seq, 1);
sockets[1].events.message({data: JSON.stringify({type: "joined", session_state: "live", history_from: 1})});
sockets[1].events.message({data: JSON.stringify({type: "caption", seq: 3})});
sockets[1].events.message({data: JSON.stringify({type: "caption", seq: 2})});
sockets[1].events.close();
assert.equal(timers.length, 1);
timers.shift()();
sockets[2].events.open();
assert.equal(sockets[2].sent[0].last_seq, 3);
'''.replace("__RESUME__", resume)
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True,
                            text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
