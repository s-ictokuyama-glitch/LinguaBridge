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


_MIC_SELECTION_SETUP = r'''
  const selected = elements.get("mic-select");
  const saved = new Map([["lb_mic_device_id", "mic-a"]]);
  const requests = [];
  const connected = new Set();
  const startInputs = [];
  const tick = () => new Promise(resolve => setImmediate(resolve));
  let resumeGate = null;

  function makeStream(deviceId, readyState = "live") {
    const track = {
      readyState,
      getSettings: () => ({deviceId}),
      stop() { this.readyState = "ended"; },
    };
    return {getAudioTracks: () => [track], getTracks: () => [track]};
  }

  function connectedDevices() {
    return [...connected]
      .flatMap(stream => stream.getAudioTracks())
      .filter(track => track.readyState === "live")
      .map(track => track.getSettings().deviceId);
  }

  selected.appendChild = () => {};
  sandbox.Option = class {};
  sandbox.localStorage = {
    getItem: key => saved.get(key) ?? null,
    setItem: (key, value) => saved.set(key, value),
  };
  sandbox.navigator.mediaDevices = {
    enumerateDevices: async () => ["mic-a", "mic-b"].map(deviceId => ({
      deviceId, kind: "audioinput", label: deviceId,
    })),
    getUserMedia({audio}) {
      const deviceId = audio.deviceId?.exact ?? "";
      const stream = makeStream(deviceId);
      return new Promise((resolve, reject) => requests.push({
        deviceId, stream, resolve: () => resolve(stream), reject,
      }));
    },
  };
  sandbox.context = {
    state: "running",
    async resume() {
      if (resumeGate) await resumeGate;
      this.state = "running";
    },
    createMediaStreamSource(stream) {
      return {
        connect() { connected.add(stream); },
        disconnect() { connected.delete(stream); },
      };
    },
  };
  sandbox.oldStream = makeStream("mic-a", "ended");
  sandbox.WebSocket.OPEN = 1;
  sandbox.ws = new sandbox.WebSocket();
  sandbox.ws.readyState = sandbox.WebSocket.OPEN;
  const send = sandbox.ws.send.bind(sandbox.ws);
  sandbox.ws.send = data => {
    const message = JSON.parse(data);
    if (message.type === "control" && message.action === "start") {
      startInputs.push(connectedDevices());
    }
    send(data);
  };
  await sandbox.init();
  await tick();
  vm.runInContext(`
    state.micReady = true;
    state.micDeviceId = "mic-a";
    state.audioCtx = context;
    state.micStream = oldStream;
    state.micSource = context.createMediaStreamSource(oldStream);
    state.micSource.connect();
    state.workletNode = {};
    state.joined = true;
    state.ws = ws;
    applySessionState("paused");
  `, sandbox);
  const clickStart = async () => {
    elements.get("start-btn").dispatchEvent(new Event("click"));
    await tick();
  };
  const selectMic = async deviceId => {
    selected.value = deviceId;
    selected.dispatchEvent(new Event("change"));
    await tick();
  };
'''


def run_mic_selection_script(body):
    run_teacher_script([
        {"status": 200, "body": {"code": "4831", "join_url": "http://lan.test/?code=4831"}},
    ], _MIC_SELECTION_SETUP + body)


@pytest.mark.parametrize("order", [[0, 1], [1, 0]])
def test_teacher_resume_follows_latest_mic_selection(order):
    run_mic_selection_script(r'''
  await clickStart();
  await selectMic("mic-b");
  assert.deepEqual(requests.map(request => request.deviceId), ["mic-a", "mic-b"]);
  for (const index of __ORDER__) {
    requests[index].resolve();
    await tick();
    if (index === 1) {
      assert.deepEqual(startInputs, [["mic-b"]], "B must start even while A is unresolved");
    } else {
      assert.equal(requests[0].stream.getAudioTracks()[0].readyState, "ended");
    }
  }
  assert.deepEqual(startInputs, [["mic-b"]]);
  assert.deepEqual(connectedDevices(), ["mic-b"]);
  assert.equal(selected.value, "mic-b");
  assert.equal(saved.get("lb_mic_device_id"), "mic-b");
'''.replace("__ORDER__", json.dumps(order)))


def test_teacher_resume_selection_failure_can_be_retried():
    run_mic_selection_script(r'''
  await clickStart();
  await selectMic("mic-b");
  requests[1].reject(new Error("selected microphone denied"));
  await tick();
  assert.deepEqual(startInputs, []);
  assert.match(elements.get("mic-status").textContent, /denied/);
  assert.equal(requests.length, 2, "failed B must not start another recovery automatically");
  await clickStart();
  assert.equal(requests.length, 3);
  requests[2].resolve();
  await tick();
  assert.deepEqual(startInputs, [["mic-a"]]);
  requests[0].resolve();
  await tick();
  assert.equal(requests[0].stream.getAudioTracks()[0].readyState, "ended");
''')


@pytest.mark.parametrize("select_before_start", [False, True])
def test_teacher_resume_observes_selection_failure_during_context_resume(select_before_start):
    run_mic_selection_script(r'''
  sandbox.context.state = "suspended";
  let resume;
  resumeGate = new Promise(resolve => { resume = resolve; });
  if (__SELECT_BEFORE_START__) await selectMic("mic-b");
  await clickStart();
  if (!__SELECT_BEFORE_START__) await selectMic("mic-b");
  assert.equal(requests.length, 1);
  requests[0].reject(new Error("selected microphone denied"));
  await tick();
  resume();
  await tick();
  assert.deepEqual(startInputs, []);
  assert.equal(requests.length, 1, "completed selection failure must not be overlooked");
  assert.match(elements.get("mic-status").textContent, /denied/);
  await clickStart();
  assert.equal(requests.length, 2);
  requests[1].resolve();
  await tick();
  assert.deepEqual(startInputs, [["mic-a"]]);
'''.replace("__SELECT_BEFORE_START__", json.dumps(select_before_start)))


def test_teacher_failed_selection_keeps_live_input():
    run_mic_selection_script(r'''
  sandbox.oldStream.getAudioTracks()[0].readyState = "live";
  await selectMic("mic-b");
  await clickStart();
  assert.deepEqual(startInputs, [], "start must wait for the pending selection");
  requests[0].reject(new Error("selected microphone denied"));
  await tick();
  assert.deepEqual(startInputs, [["mic-a"]]);
  assert.equal(selected.value, "mic-a");
  assert.equal(saved.get("lb_mic_device_id"), "mic-a");
  assert.match(elements.get("mic-notice").textContent, /使えませんでした/);
''')
