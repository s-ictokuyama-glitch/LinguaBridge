"""Opt-in evidence checks that use this Windows PC's physical peripherals.

The microphone check never persists or transcribes captured audio.  The real
browser sends its normal PCM frames over a loopback WebSocket, whose receiver
records frame lengths only and immediately discards the bytes.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import pytest
import uvicorn

from server.asr.fake_engine import FakeASREngine
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE, make_ws_test_config


RUN_PHYSICAL_MIC = os.environ.get("LINGUABRIDGE_RUN_PHYSICAL_MIC_QA") == "1"
EDGE = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")

CDP_CHECK = r"""
const debugPort = process.argv[1];
const targetUrl = process.argv[2];
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function waitForDebugger() {
  let lastError;
  for (let attempt = 0; attempt < 100; attempt++) {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/version`);
      if (response.ok) return;
      lastError = new Error(`debugger readiness returned HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await pause(100);
  }
  throw new Error(`Edge debugger did not appear: ${lastError}`);
}

async function createOwnedTarget() {
  await waitForDebugger();
  const response = await fetch(
    `http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent('about:blank')}`,
    {method: 'PUT'},
  );
  if (!response.ok) {
    throw new Error(`creating dedicated Edge target returned HTTP ${response.status}`);
  }
  const target = await response.json();
  if (target.type !== 'page' || !target.id || !target.webSocketDebuggerUrl) {
    throw new Error(`invalid dedicated Edge target: ${JSON.stringify(target)}`);
  }
  return target;
}

const target = await createOwnedTarget();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  ws.addEventListener('open', resolve, {once: true});
  ws.addEventListener('error', reject, {once: true});
});
let nextId = 0;
const pending = new Map();
ws.addEventListener('message', async event => {
  const raw = typeof event.data === 'string' ? event.data : await event.data.text();
  const message = JSON.parse(raw);
  if (!message.id || !pending.has(message.id)) return;
  const waiter = pending.get(message.id);
  pending.delete(message.id);
  clearTimeout(waiter.timer);
  if (message.error) waiter.reject(new Error(JSON.stringify(message.error)));
  else waiter.resolve(message.result);
});
ws.addEventListener('close', event => {
  for (const waiter of pending.values()) {
    clearTimeout(waiter.timer);
    waiter.reject(new Error(
      `CDP socket for target ${target.id} (${target.url}) closed during ` +
      `${waiter.method}: ${event.code} ${event.reason}`,
    ));
  }
  pending.clear();
});

function call(method, params = {}) {
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`CDP command timed out: ${method}`));
    }, 5000);
    pending.set(id, {resolve, reject, timer, method});
    try {
      ws.send(JSON.stringify({id, method, params}));
    } catch (error) {
      clearTimeout(timer);
      pending.delete(id);
      reject(error);
    }
  });
}

async function evaluate(expression, userGesture = false) {
  const reply = await call('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture,
  });
  if (reply.exceptionDetails) throw new Error(JSON.stringify(reply.exceptionDetails));
  return reply.result.value;
}

async function waitFor(expression, description) {
  for (let attempt = 0; attempt < 150; attempt++) {
    if (await evaluate(`Boolean(${expression})`)) return;
    await pause(100);
  }
  throw new Error(`Timed out waiting for ${description}`);
}

await call('Runtime.enable');
await call('Page.enable');
await call('Page.navigate', {url: targetUrl});
await waitFor("typeof state !== 'undefined' && state.joined", 'teacher join');
await evaluate("document.querySelector('#start-btn').click(); true", true);
await waitFor(
  "state.micReady && state.sessionState === 'live' && state.audioCtx",
  'physical microphone capture',
);
await pause(2500);
const observed = await evaluate(`({
  secure: window.isSecureContext,
  micReady: state.micReady,
  sessionState: state.sessionState,
  audioState: state.audioCtx.state,
  sampleRate: state.audioCtx.sampleRate,
  micStatus: document.querySelector('#mic-status').textContent,
  tracks: state.micStream.getAudioTracks().map(track => ({
    label: track.label,
    readyState: track.readyState,
    muted: track.muted,
  })),
})`);
ws.close();
console.log(JSON.stringify(observed));
"""


@contextmanager
def _discarding_loopback_server(tmp_path: Path):
    """Run the real HTTP/WS app while discarding physical PCM on receipt."""

    recording_dir = tmp_path / "recordings"
    config = make_ws_test_config()
    config.recording.out_dir = str(recording_dir)
    app = create_app(
        config,
        asr_engine=FakeASREngine(),
        mt_engine=FakeTranslationEngine(),
        join_code=JOIN_CODE,
    )
    received_frame_lengths: list[int] = []

    async def discard_audio(data: bytes) -> None:
        received_frame_lengths.append(len(data))

    app.state.pipeline.feed_audio = discard_audio
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            server.run(sockets=[listener])
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if errors or not thread.is_alive() or time.monotonic() >= deadline:
            raise RuntimeError(f"loopback server failed to start: {errors!r}")
        time.sleep(0.02)
    try:
        yield port, received_frame_lengths, recording_dir
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive(), "loopback server failed to stop"
        assert not errors, errors


@contextmanager
def _local_edge_profile():
    """Keep Edge's sandboxed caches off OneDrive-backed workspace paths."""

    with tempfile.TemporaryDirectory(prefix="linguabridge-physical-mic-") as directory:
        yield Path(directory)


def _available_loopback_port() -> int:
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


@pytest.mark.skipif(
    sys.platform != "win32" or not RUN_PHYSICAL_MIC,
    reason="set LINGUABRIDGE_RUN_PHYSICAL_MIC_QA=1 on the audited Windows PC",
)
@pytest.mark.timeout(45)
def test_teach01_physical_microphone_sends_pcm_frames_over_loopback_ws_without_persistence(
    tmp_path: Path,
) -> None:
    """Real Edge/mic/API/Worklet/WS; discard frame bytes before VAD or ASR."""

    assert EDGE.is_file(), f"Microsoft Edge not found: {EDGE}"
    node = shutil.which("node")
    assert node is not None, "Node.js is required for dependency-free Edge CDP control"

    with (
        _local_edge_profile() as profile,
        _discarding_loopback_server(tmp_path) as (port, frame_lengths, recording_dir),
    ):
        teacher_url = f"http://127.0.0.1:{port}/teacher"
        debug_port = _available_loopback_port()
        browser = subprocess.Popen(
            [
                str(EDGE),
                "--headless=new",
                f"--remote-debugging-port={debug_port}",
                "--remote-allow-origins=*",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--disable-default-apps",
                "--autoplay-policy=no-user-gesture-required",
                "--use-fake-ui-for-media-stream",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        browser_returncode_before_cleanup: int | None = None
        debugger_status = "not checked"
        browser_stderr = ""
        try:
            result = subprocess.run(
                [node, "--input-type=module", "-e", CDP_CHECK, str(debug_port), teacher_url],
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=25,
                check=False,
            )
            browser_returncode_before_cleanup = browser.poll()
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{debug_port}/json/version", timeout=2
                ) as response:
                    debugger_status = f"HTTP {response.status}"
            except Exception as exc:  # pragma: no cover - diagnostic failure path
                debugger_status = f"{type(exc).__name__}: {exc}"
        finally:
            if browser.poll() is None:
                browser.terminate()
            try:
                _, browser_stderr = browser.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                browser.kill()
                _, browser_stderr = browser.communicate(timeout=5)

    assert result.returncode == 0, (
        f"{result.stdout}\n"
        f"browser_returncode_before_cleanup={browser_returncode_before_cleanup!r}\n"
        f"debugger_status_after_cdp={debugger_status}\n"
        f"edge_stderr={browser_stderr}"
    )
    observed = json.loads(result.stdout.splitlines()[-1])
    print(
        "TEACH01_PHYSICAL_MIC_OBSERVATION="
        + json.dumps(
            {
                "secure": observed["secure"],
                "mic_ready": observed["micReady"],
                "session_state": observed["sessionState"],
                "audio_state": observed["audioState"],
                "sample_rate": observed["sampleRate"],
                "track_labels": [track["label"] for track in observed["tracks"]],
                "frame_count": len(frame_lengths),
                "frame_lengths": sorted(set(frame_lengths)),
                "recording_directory_created": recording_dir.exists(),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    assert observed["secure"] is True
    assert observed["micReady"] is True
    assert observed["sessionState"] == "live"
    assert observed["audioState"] == "running"
    assert observed["tracks"]
    assert all(track["readyState"] == "live" for track in observed["tracks"])
    assert all(
        track["label"] and not track["label"].startswith("Fake ")
        for track in observed["tracks"]
    )
    assert len(frame_lengths) >= 5, "physical AudioWorklet sent no loopback WS frames"
    assert set(frame_lengths) == {3200}
    assert not recording_dir.exists(), "recording-off microphone evidence persisted data"
