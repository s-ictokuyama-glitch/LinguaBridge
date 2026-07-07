// 生徒ページ: コード入力（QRクエリで自動入力）→ 言語選択 → 字幕カード表示
"use strict";

const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 15000;

const state = {
  code: "",
  lang: null,
  prevLang: null,
  lastSeq: 0,
  ws: null,
  joined: false,
  ended: false,
  languages: [],
  retryDelayMs: RETRY_BASE_MS,
};

const el = {
  joinScreen: document.getElementById("join-screen"),
  captionScreen: document.getElementById("caption-screen"),
  codeInput: document.getElementById("code-input"),
  langOptions: document.getElementById("lang-options"),
  joinBtn: document.getElementById("join-btn"),
  joinError: document.getElementById("join-error"),
  banner: document.getElementById("session-banner"),
  langSelect: document.getElementById("lang-select"),
  cards: document.getElementById("cards"),
};

const BANNERS = {
  idle: ["idle", "開始待ち / Waiting"],
  live: ["live", "配信中 / Live"],
  paused: ["paused", "一時停止中 / Paused"],
  ended: ["ended", "授業は終了しました / Ended"],
  disconnected: ["disconnected", "再接続中… / Reconnecting…"],
};

function setBanner(key) {
  const [cls, text] = BANNERS[key] || BANNERS.idle;
  el.banner.className = `banner ${cls}`;
  el.banner.textContent = text;
}

function updateJoinButton() {
  el.joinBtn.disabled = !(el.codeInput.value.trim().length === 4 && state.lang);
}

async function init() {
  const params = new URLSearchParams(location.search);
  if (params.get("code")) el.codeInput.value = params.get("code");

  const res = await fetch("/api/config");
  const cfg = await res.json();
  state.languages = cfg.languages;
  for (const lang of cfg.languages) {
    const btn = document.createElement("button");
    btn.textContent = lang.label;
    btn.dataset.code = lang.code;
    btn.addEventListener("click", () => {
      state.lang = lang.code;
      for (const b of el.langOptions.children) b.classList.toggle("selected", b === btn);
      updateJoinButton();
    });
    el.langOptions.appendChild(btn);

    const opt = document.createElement("option");
    opt.value = lang.code;
    opt.textContent = lang.label;
    el.langSelect.appendChild(opt);
  }

  el.codeInput.addEventListener("input", updateJoinButton);
  el.joinBtn.addEventListener("click", () => {
    state.code = el.codeInput.value.trim();
    el.joinError.hidden = true;
    connect(null);
  });
  el.langSelect.addEventListener("change", () => {
    state.prevLang = state.lang;
    state.lang = el.langSelect.value;
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({ type: "set_lang", lang: state.lang }));
    }
  });
}

function connect(lastSeq) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;

  ws.addEventListener("open", () => {
    const msg = { type: "join", role: "student", code: state.code, lang: state.lang };
    if (lastSeq !== null && lastSeq !== undefined) msg.last_seq = lastSeq;
    ws.send(JSON.stringify(msg));
  });

  ws.addEventListener("message", (ev) => handleMessage(JSON.parse(ev.data)));

  ws.addEventListener("close", () => {
    if (!state.joined || state.ended) return;
    setBanner("disconnected");
    // 指数バックオフで自動再接続し、last_seq で欠落分を差分復元する（E-06）
    setTimeout(() => connect(state.lastSeq), state.retryDelayMs);
    state.retryDelayMs = Math.min(state.retryDelayMs * 2, RETRY_MAX_MS);
  });
}

function handleMessage(msg) {
  switch (msg.type) {
    case "joined":
      state.joined = true;
      state.retryDelayMs = RETRY_BASE_MS; // 再接続成功でバックオフをリセット
      el.joinScreen.hidden = true;
      el.captionScreen.hidden = false;
      el.langSelect.value = state.lang;
      setBanner(msg.session_state);
      break;
    case "join_rejected":
      el.joinError.textContent =
        msg.reason === "bad_code"
          ? "参加コードが違います / Wrong code / 参加码错误"
          : msg.reason === "rate_limited"
            ? "試行回数が多すぎます。1分ほど待ってください / Too many attempts"
            : "この言語には対応していません / Unsupported language";
      el.joinError.hidden = false;
      break;
    case "caption":
      addCard(msg);
      if (msg.seq > state.lastSeq) state.lastSeq = msg.seq;
      break;
    case "session":
      if (msg.state === "ended") state.ended = true;
      setBanner(msg.state);
      break;
    case "error":
      // set_lang が拒否された場合は選択を元に戻す
      if (msg.code === "bad_lang" && state.prevLang) {
        state.lang = state.prevLang;
        el.langSelect.value = state.prevLang;
      }
      break;
  }
}

function addCard(msg) {
  if (el.cards.querySelector(`[data-seq="${msg.seq}"]`)) return; // 再送の重複防御
  const nearBottom =
    el.cards.scrollHeight - el.cards.scrollTop - el.cards.clientHeight < 120;
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.seq = msg.seq;
  const text = document.createElement("p");
  text.className = "text";
  text.textContent = msg.text;
  const ja = document.createElement("p");
  ja.className = "ja";
  ja.textContent = msg.ja;
  card.append(text, ja);
  // 再接続復元と新着が交錯しても表示は発話順を保つ（seq昇順の位置に挿入）
  let ref = null;
  for (let node = el.cards.lastElementChild; node; node = node.previousElementSibling) {
    if (Number(node.dataset.seq) < msg.seq) break;
    ref = node;
  }
  el.cards.insertBefore(card, ref);
  // 最下部付近を見ている時だけ自動スクロール（履歴を遡っている間は追従しない）
  if (nearBottom) el.cards.scrollTop = el.cards.scrollHeight;
}

init();
