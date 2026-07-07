// 生徒ページ: コード入力（QRクエリで自動入力）→ 言語選択 → 字幕カード表示
"use strict";

const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 15000;

const state = {
  code: "",
  lang: null,
  prevLang: null,
  // 連続確定watermark: 「ここまでは1つも欠けずに受信済み」のseq。
  // 再接続時の last_seq に使う（表示済みの最大seqではない — 復元とライブの
  // 交錯で歯抜けのまま最大値を申告すると、間のseqが恒久欠落するため）
  lastSeq: 0,
  pendingSeqs: new Set(), // watermarkより先に届いたseq（連続がつながるまで保持）
  ws: null,
  joined: false,
  ended: false,
  languages: [],
  retryDelayMs: RETRY_BASE_MS,
};

function advanceWatermark(seq) {
  if (seq <= state.lastSeq) return;
  state.pendingSeqs.add(seq);
  while (state.pendingSeqs.has(state.lastSeq + 1)) {
    state.pendingSeqs.delete(state.lastSeq + 1);
    state.lastSeq += 1;
  }
}

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
    // 指数バックオフ＋ジッタで自動再接続し、last_seq で欠落分を差分復元する（E-06）。
    // ジッタはAP瞬断復帰時に全端末が同時再接続するのを避けるため
    const jitter = 0.7 + Math.random() * 0.6;
    setTimeout(() => connect(state.lastSeq), state.retryDelayMs * jitter);
    state.retryDelayMs = Math.min(state.retryDelayMs * 2, RETRY_MAX_MS);
  });
}

function handleMessage(msg) {
  switch (msg.type) {
    case "joined":
      state.joined = true;
      state.retryDelayMs = RETRY_BASE_MS; // 再接続成功でバックオフをリセット
      // 履歴上限で復元不能になった分は欠落確定として watermark を進める
      if (msg.history_from - 1 > state.lastSeq) {
        state.lastSeq = msg.history_from - 1;
        state.pendingSeqs.clear();
      }
      el.joinScreen.hidden = true;
      el.captionScreen.hidden = false;
      el.langSelect.value = state.lang;
      setBanner(msg.session_state);
      break;
    case "join_rejected": {
      if (state.joined && msg.reason === "rate_limited") {
        // 再接続中の一時ブロック: 接続を切ってバックオフ再試行に任せる
        state.ws.close();
        break;
      }
      if (state.joined) {
        // 再接続中にコードが無効化された（サーバー再起動等）: 参加画面へ戻す
        state.joined = false;
        el.captionScreen.hidden = true;
        el.joinScreen.hidden = false;
      }
      el.joinError.textContent =
        msg.reason === "bad_code"
          ? "参加コードが違います / Wrong code / 参加码错误"
          : msg.reason === "rate_limited"
            ? "試行回数が多すぎます。1分ほど待ってください / Too many attempts"
            : "この言語には対応していません / Unsupported language";
      el.joinError.hidden = false;
      break;
    }
    case "caption":
      addCard(msg);
      advanceWatermark(msg.seq);
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
