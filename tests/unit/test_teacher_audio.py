"""先生の再開操作で音声入力が復旧し、失敗時は配信を始めないことを確認する。"""

import json
import shutil
import subprocess

import pytest

from tests.teacher_page import ROOT, run_teacher_script


def test_teacher_audio_capture_histories(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("先生画面のJavaScript検証にはNode.jsが必要")
    result = subprocess.run(
        [node, "scripts/formal/teacher_audio.cjs", "--seconds", "5",
         "--pcm", str(tmp_path / "audio.pcm")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "PASS"
    assert report["bounded"]["legal"] == report["bounded"]["passed"] == 24
    assert report["sustained"]["status"] == "PASS"


@pytest.mark.parametrize("failure", ["resume", "getUserMedia"])
def test_teacher_resume_failure_can_be_retried(failure):
    run_teacher_script([
        {"status": 200, "body": {"code": "4831", "join_url": "http://lan.test/?code=4831"}},
    ], r'''
  await sandbox.init();
  let reject = true;
  const failure = __FAILURE__;
  const track = {readyState: failure === "resume" ? "live" : "ended", stop() {}};
  const context = {
    state: failure === "resume" ? "suspended" : "running",
    async resume() {
      if (reject) throw new Error("audio resume denied");
      this.state = "running";
    },
    createMediaStreamSource() { return {connect() {}}; },
  };
  sandbox.context = context;
  sandbox.track = track;
  sandbox.WebSocket.OPEN = 1;
  sandbox.navigator.mediaDevices = {
    async getUserMedia() {
      if (reject) throw new Error("microphone denied");
      const liveTrack = {readyState: "live", stop() { this.readyState = "ended"; }};
      return {getAudioTracks: () => [liveTrack], getTracks: () => [liveTrack]};
    },
  };
  vm.runInContext(`
    state.micReady = true;
    state.audioCtx = context;
    state.micStream = {getAudioTracks: () => [track], getTracks: () => [track]};
    state.joined = true;
    state.sessionState = "paused";
    state.ws = new WebSocket();
    state.ws.readyState = WebSocket.OPEN;
  `, sandbox);
  const clickStart = async () => {
    elements.get("start-btn").dispatchEvent(new Event("click"));
    await new Promise(resolve => setImmediate(resolve));
  };
  await clickStart();
  assert.deepEqual(sent, [], "failed capture recovery must not send start");
  assert.match(elements.get("mic-status").textContent, /denied/);
  reject = false;
  await clickStart();
  assert.deepEqual(sent, [{type: "control", action: "start"}]);
  assert.equal(elements.get("mic-status").textContent, "マイク取得済み。");
'''.replace("__FAILURE__", json.dumps(failure)))
