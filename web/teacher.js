// 先生ページ: 参加情報（QR・コード）表示、マイク→AudioWorklet→WS送信、配信制御、
// モニタリング（統計・入力レベル・無音/過負荷警告）。
// #10 では localhost で開く運用（getUserMedia のセキュアコンテキスト要件）。
"use strict";

const MIC_LEVEL_FULL_SCALE = 3000; // このRMS(int16)でメーター満杯とみなす
const STORAGE_MIC_DEVICE = "lb_mic_device_id"; // 選んだマイク（#30）。生徒側の作法に揃える

const state = {
  ws: null,
  joined: false,
  sessionState: "idle",
  micReady: false,
  audioCtx: null,
  workletNode: null,
  micSource: null, // 現在つないでいる MediaStreamSource
  micStream: null, // 現在の MediaStream（切替時に track を止める）
  micDeviceId: "", // 空文字 = 既定のマイク
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
  micSelect: document.getElementById("mic-select"),
  micNotice: document.getElementById("mic-notice"),
  recordToggle: document.getElementById("record-toggle"),
  recordIndicator: document.getElementById("record-indicator"),
  micMeterBar: document.getElementById("mic-meter-bar"),
  silenceWarning: document.getElementById("silence-warning"),
  overloadWarning: document.getElementById("overload-warning"),
  statStudents: document.getElementById("stat-students"),
  statLangs: document.getElementById("stat-langs"),
  statDelay: document.getElementById("stat-delay"),
  statAsrWait: document.getElementById("stat-asr-wait"),
  statAsrActive: document.getElementById("stat-asr-active"),
  statAsrMs: document.getElementById("stat-asr-ms"),
  statMtQueue: document.getElementById("stat-mt-queue"),
  statMtMs: document.getElementById("stat-mt-ms"),
  statCache: document.getElementById("stat-cache"),
  transcript: document.getElementById("transcript"),
  partial: document.getElementById("partial"),
};

// 表示中の partial の turn_id（#29）。同じ turn の asr_final が来たら消す
let partialTurnId = null;

function showPartial(msg) {
  partialTurnId = msg.turn_id;
  el.partial.textContent = msg.ja;
  el.partial.hidden = false;
}

function clearPartial(turnId) {
  // turnId 指定時は、その turn の partial を表示中のときだけ消す。
  // 指定なし（speaking:false・一時停止など）は無条件に消す
  if (turnId !== undefined && partialTurnId !== turnId) return;
  partialTurnId = null;
  el.partial.textContent = "";
  el.partial.hidden = true;
}

const STATE_LABELS = { idle: "未開始", live: "配信中", paused: "一時停止中", ended: "終了" };

// ---- マイク選択（#30。再読み込み後も保持する: 生徒側 F-06 と同じ作法） ----
// localStorage はプライベートブラウズ等で例外を投げるため保護する
// （ここで初期化が死ぬと配信そのものが始められなくなる）

function storageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* 保存できなくても選択そのものは効く */
  }
}

function showMicNotice(text) {
  el.micNotice.textContent = text ? `⚠ ${text}` : "";
  el.micNotice.hidden = !text;
}

/** 入力デバイスを列挙して select を埋める。
 *
 * 権限取得前はブラウザが label を空文字で返す（deviceId も出さないことがある）ので、
 * **label が1つでも取れているか**を「許可済み」の判定に使う。未許可のうちは
 * 一覧を出しても意味のある選択ができないため select を無効のままにする。
 */
async function refreshMicList() {
  let devices = [];
  try {
    devices = await navigator.mediaDevices.enumerateDevices();
  } catch {
    return; // 列挙できない環境では既定のマイクのまま動かす
  }
  const inputs = devices.filter((d) => d.kind === "audioinput");
  const granted = inputs.some((d) => d.label);
  el.micSelect.textContent = "";
  el.micSelect.appendChild(new Option("既定のマイク", ""));
  for (const d of inputs) {
    if (!d.deviceId || d.deviceId === "default") continue; // 先頭の既定項目と重複する
    el.micSelect.appendChild(new Option(d.label || "マイク", d.deviceId));
  }
  el.micSelect.disabled = !granted;
  if (!granted) {
    el.micSelect.value = "";
    return;
  }
  // 保存された選択を復元する。見つからなければ既定へ落とすが、**黙って落とさない** —
  // ピンマイクのつもりで内蔵マイクが使われるのが、この機能で一番まずい失敗
  const wanted = state.micReady ? state.micDeviceId : storageGet(STORAGE_MIC_DEVICE) || "";
  const found = wanted === "" || inputs.some((d) => d.deviceId === wanted);
  el.micSelect.value = found ? wanted : "";
  if (!found) {
    state.micDeviceId = "";
    storageSet(STORAGE_MIC_DEVICE, "");
    showMicNotice("前回選んだマイクが見つかりません。既定のマイクを使います。");
  }
}

/** 指定デバイスのマイクストリームを取る（グラフには触らない）。 */
function getMicStream(deviceId) {
  const audio = {
    channelCount: 1,
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  };
  // exact にするのは、開けなかったときに例外にしたいから。制約を緩めると
  // ブラウザが黙って別のマイクを選び、先生は気付けない
  if (deviceId) audio.deviceId = { exact: deviceId };
  return navigator.mediaDevices.getUserMedia({ audio });
}

/** 取得済みのストリームをワークレットへつなぎ替える。
 *
 * 「新しい source を connect → 旧 source を disconnect → 旧 track を stop」の順で行う。
 * `getUserMedia` の await はこの前に済んでいて、その間は旧マイクが生きたままなので、
 * 切替で無音の穴を作らない。
 * `AudioContext` と `AudioWorkletNode` は使い回す — ワークレットの `sampleRate` は
 * 構築時に固定されており、作り直すと発話の取り込み経路ごと入れ替わってしまう。
 */
function attachStream(stream, deviceId) {
  const source = state.audioCtx.createMediaStreamSource(stream);
  source.connect(state.workletNode);
  if (state.micSource) state.micSource.disconnect();
  if (state.micStream) state.micStream.getTracks().forEach((t) => t.stop());
  state.micSource = source;
  state.micStream = stream;
  state.micDeviceId = deviceId;
  storageSet(STORAGE_MIC_DEVICE, deviceId);
}

/** select の操作。失敗したら選択を元へ戻し、理由を出す。 */
async function onMicSelected() {
  const wanted = el.micSelect.value;
  const previous = state.micDeviceId;
  if (!state.micReady || wanted === previous) return;
  try {
    attachStream(await getMicStream(wanted), wanted);
    showMicNotice("");
    el.micStatus.textContent = "マイクを切り替えました。";
  } catch (err) {
    el.micSelect.value = previous;
    showMicNotice(`このマイクを使えませんでした（${err.name}）。前のマイクのままです。`);
  }
}

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
  el.recordToggle.addEventListener("change", () => {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({ type: "recording", on: el.recordToggle.checked }));
    }
  });

  // マイク選択（#30）。許可済みなら再読み込み直後から一覧が出る
  el.micSelect.addEventListener("change", onMicSelected);
  if (navigator.mediaDevices) {
    navigator.mediaDevices.addEventListener?.("devicechange", refreshMicList);
    refreshMicList(); // ピンマイクの抜き差しにも追随する
  }
}

function applyRecording(on) {
  el.recordIndicator.hidden = !on;
  el.recordToggle.checked = on; // サーバーの状態を正とする
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
    state.joined = false; // 再接続の "joined" で立て直す。切断中は操作させない
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
      applySessionState(msg.session_state); // ボタンの活性・ラベルもここで決まる
      applyRecording(msg.recording);
      break;
    case "recording":
      applyRecording(msg.on);
      break;
    case "join_rejected":
      el.pageError.textContent =
        "サーバーへの参加が拒否されました。サーバーを再起動してページを開き直してください。";
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
      clearPartial(msg.turn_id); // 暫定行を確定カードで置き換える（#29）
      el.silenceWarning.hidden = true; // 発話が届いた＝マイクは生きている
      break;
    }
    case "turn.partial":
      showPartial(msg);
      break;
    case "speaking":
      // 発話が途切れた。確定しなかった partial（幻覚破棄など）が残らないようにする
      if (!msg.on) clearPartial();
      break;
    case "stats":
      applyStats(msg);
      break;
    case "error":
      if (msg.code === "mic_silent") {
        el.silenceWarning.textContent = `⚠ ${msg.message}`;
        el.silenceWarning.hidden = false;
      } else {
        el.micStatus.textContent = `サーバーからの警告: ${msg.message}`;
      }
      break;
  }
}

function applyStats(msg) {
  el.statStudents.textContent = String(msg.students);
  const langs = Object.entries(msg.langs);
  el.statLangs.textContent = langs.length
    ? langs.map(([code, n]) => `${code}: ${n}`).join(" / ")
    : "—";
  el.statDelay.textContent = String(msg.median_delay_ms);
  // 遅延の内訳（#30）。「音声待ち」と「処理中」は必ず分けて出す:
  // 合算（audio_queue_seconds）だけを見せると、1発話が長いだけの状態を
  // 滞留と誤読させる（#25・#29 で二度確認された読み間違い）
  el.statAsrWait.textContent = (msg.asr_wait_seconds ?? 0).toFixed(1);
  el.statAsrActive.textContent = (msg.asr_active_seconds ?? 0).toFixed(1);
  el.statAsrMs.textContent = String(msg.median_asr_ms ?? 0);
  el.statMtQueue.textContent = String(msg.mt_queue_depth ?? 0);
  el.statMtMs.textContent = String(msg.median_mt_ms ?? 0);
  // 発話をまたぐ翻訳キャッシュ（#26）。件数も出す（率だけだと母数が見えない）
  const hits = msg.mt_cache_hits ?? 0;
  const rate = Math.round((msg.mt_cache_hit_rate ?? 0) * 100);
  el.statCache.textContent = hits ? `${rate}% (${hits}件節約)` : "—";
  el.overloadWarning.hidden = !msg.overloaded;
}

function applySessionState(s) {
  state.sessionState = s;
  el.sessionState.textContent = STATE_LABELS[s] || s;
  if (s !== "live") {
    el.silenceWarning.hidden = true;
    el.overloadWarning.hidden = true;
    clearPartial(); // 一時停止・終了で暫定行を残さない（#29）
  }
  setButtons(state.joined);
}

/** ボタンのラベルと活性を状態から決める（#30）。
 *
 * 一時停止からの再開は「開始」の押し直しで行われる（サーバー側は `start` のまま）。
 * ミッションが start / pause / **resume** / end を挙げているので、
 * **押す前に何が起きるか**がボタンから読めるようラベルを切り替える。
 * ended から開始し直せる現行の挙動は残す。
 */
function setButtons(connected) {
  const s = state.sessionState;
  el.startBtn.textContent = s === "paused" ? "再開" : "開始";
  el.startBtn.disabled = !connected || s === "live";
  el.pauseBtn.disabled = !connected || s !== "live";
  el.endBtn.disabled = !connected || (s !== "live" && s !== "paused");
}

function sendControl(action) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: "control", action }));
  }
}

function updateMicMeter(buffer) {
  const samples = new Int16Array(buffer);
  let sumSq = 0;
  for (let i = 0; i < samples.length; i++) sumSq += samples[i] * samples[i];
  const rms = Math.sqrt(sumSq / samples.length);
  const level = Math.min(1, rms / MIC_LEVEL_FULL_SCALE);
  el.micMeterBar.style.width = `${(level * 100).toFixed(0)}%`;
  el.micMeterBar.classList.toggle("silent", level < 0.02);
}

/** マイクの許可を取り、音声グラフを組み立てる（配信開始時に一度だけ）。
 *
 * デバイスの取得（`getMicStream`）とグラフへの接続（`attachStream`）に分けてあるので、
 * 配信中のマイク切替は後者だけをやり直せばよく、ワークレットを作り直さずに済む（#30）。
 */
async function ensureMic() {
  if (state.micReady) return;
  // **マイクを取ってから**音声グラフを作る。`AudioContext.resume()` はユーザー操作の
  // 文脈でしか解決しないうえ、許可が下りなければグラフは要らない。
  // 許可を拒否されたあと「開始」を押し直すとここへ戻ってくるので、
  // グラフの構築は一度きりにする（毎回作ると AudioContext が積み上がる）
  const wanted = storageGet(STORAGE_MIC_DEVICE) || "";
  let deviceId = wanted;
  let stream;
  try {
    stream = await getMicStream(wanted);
  } catch (err) {
    if (!wanted) throw err; // 既定のマイクすら取れない＝呼び出し側で理由を出す
    // 前回のマイクが無くなっただけ。許可の取得ごと失敗させずに既定でやり直す
    stream = await getMicStream("");
    deviceId = "";
    showMicNotice("前回選んだマイクを使えませんでした。既定のマイクを使います。");
  }
  if (state.audioCtx === null) {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    await ctx.resume();
    await ctx.audioWorklet.addModule("/static/audio-worklet.js");
    const node = new AudioWorkletNode(ctx, "pcm16-downsampler");
    node.port.onmessage = (ev) => {
      updateMicMeter(ev.data); // メーターは live 以外でも常時更新（無音の視認 E-01）
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
    node.connect(mute);
    mute.connect(ctx.destination);
    state.audioCtx = ctx;
    state.workletNode = node;
  }
  attachStream(stream, deviceId);
  state.micReady = true;
  el.micStatus.textContent = "マイク取得済み。";
  await refreshMicList(); // 許可が下りたのでラベル付きの一覧が取れる
}

init();
