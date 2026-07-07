// 先生ページ: 参加情報（QR・コード）表示、マイク→AudioWorklet→WS送信、配信制御
// #10 では localhost で開く運用（getUserMedia のセキュアコンテキスト要件）。
"use strict";

const state = {
  ws: null,
  joined: false,
  sessionState: "idle",
  micReady: false,
  audioCtx: null,
};

const el = {
  pageError: document.getElementById("page-error"),
  qr: document.getElementById("qr"),
  joinCode: document.getElementById("join-code"),
  joinUrl: document.getElementById("join-url"),
  sessionState: document.getElementById("session-state"),
  startBtn: document.getElementById("start-btn"),
  pauseBtn: document.getElementById("pause-btn"),
  endBtn: document.getElementById("end-btn"),
  micStatus: document.getElementById("mic-status"),
  transcript: document.getElementById("transcript"),
};

const STATE_LABELS = { idle: "未開始", live: "配信中", paused: "一時停止中", ended: "終了" };

async function init() {
  const res = await fetch("/api/teacher-info");
  if (!res.ok) {
    el.pageError.textContent =
      "先生ページはサーバーPC上で http://127.0.0.1:8000/teacher を開いてください。";
    el.pageError.hidden = false;
    return;
  }
  const info = await res.json();
  el.joinCode.textContent = info.code;
  el.joinUrl.textContent = info.join_url;
  new QRCode(el.qr, { text: info.join_url, width: 200, height: 200 });

  connect(info.code);

  el.startBtn.addEventListener("click", async () => {
    try {
      await ensureMic();
    } catch (err) {
      el.micStatus.textContent = `マイクを取得できませんでした: ${err.message}`;
      return;
    }
    sendControl("start");
  });
  el.pauseBtn.addEventListener("click", () => sendControl("pause"));
  el.endBtn.addEventListener("click", () => sendControl("end"));
}

function connect(code) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;
  ws.addEventListener("open", () => {
    ws.send(JSON.stringify({ type: "join", role: "teacher", code }));
  });
  ws.addEventListener("message", (ev) => handleMessage(JSON.parse(ev.data)));
  ws.addEventListener("close", (ev) => {
    if (!state.joined) return;
    setButtons(false);
    if (ev.code === 4000) {
      // 後勝ち接続に置き換えられた（E-08）。再接続すると互いにキックし合うので止まる
      el.pageError.textContent =
        "別のタブ・端末で先生ページが接続されたため、この接続は終了しました。";
      el.pageError.hidden = false;
      return;
    }
    el.micStatus.textContent = "サーバーとの接続が切れました。再接続中…";
    setTimeout(() => connect(code), 2000);
  });
}

function handleMessage(msg) {
  switch (msg.type) {
    case "joined":
      state.joined = true;
      applySessionState(msg.session_state);
      setButtons(true);
      break;
    case "join_rejected":
      el.pageError.textContent = "サーバーへの参加が拒否されました。サーバーを再起動してページを開き直してください。";
      el.pageError.hidden = false;
      break;
    case "session":
      applySessionState(msg.state);
      break;
    case "asr_final": {
      const li = document.createElement("li");
      li.textContent = `#${msg.seq} ${msg.ja}`;
      el.transcript.appendChild(li);
      while (el.transcript.children.length > 50) el.transcript.firstChild.remove();
      el.transcript.scrollTop = el.transcript.scrollHeight;
      break;
    }
    case "error":
      el.micStatus.textContent = `サーバーからの警告: ${msg.message}`;
      break;
  }
}

function applySessionState(s) {
  state.sessionState = s;
  el.sessionState.textContent = STATE_LABELS[s] || s;
}

function setButtons(enabled) {
  el.startBtn.disabled = !enabled;
  el.pauseBtn.disabled = !enabled;
  el.endBtn.disabled = !enabled;
}

function sendControl(action) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: "control", action }));
  }
}

async function ensureMic() {
  if (state.micReady) return;
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  await ctx.resume();
  await ctx.audioWorklet.addModule("/static/audio-worklet.js");
  const source = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, "pcm16-downsampler");
  node.port.onmessage = (ev) => {
    // サーバー側でも live 以外は破棄するが、無駄な送信を避ける
    if (
      state.sessionState === "live" &&
      state.ws &&
      state.ws.readyState === WebSocket.OPEN
    ) {
      state.ws.send(ev.data);
    }
  };
  const mute = ctx.createGain();
  mute.gain.value = 0; // ワークレットをグラフに保持しつつスピーカーには出さない
  source.connect(node);
  node.connect(mute);
  mute.connect(ctx.destination);
  state.audioCtx = ctx;
  state.micReady = true;
  el.micStatus.textContent = "マイク取得済み。";
}

init();
