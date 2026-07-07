// 生徒ページ: コード入力（QRクエリで自動入力）→ 言語選択 → 字幕カード表示
// UI文言は i18n.js（ja/en/zh）で選択言語に追従する（F-07）
"use strict";

const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 15000;
const DELAY_NOTICE_MS = 5000; // これ以上遅れた字幕に「遅延中」を付す（E-05）
const FOLLOW_THRESHOLD_PX = 40; // 最下部からこの範囲内なら「追従中」とみなす
const STORAGE_FONT_SIZE = "lb_font_size";
const STORAGE_SHOW_JA = "lb_show_ja";

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
  following: true, // 自動スクロール追従中か（上スクロールで停止、「最新へ」で復帰）
  bannerKey: "idle",
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
  jaToggle: document.getElementById("ja-toggle"),
  sizeButtons: document.getElementById("size-buttons"),
  latestBtn: document.getElementById("latest-btn"),
};

// ---- i18n ----

function t(key) {
  const dict = I18N[state.lang] || I18N.ja;
  return dict[key] || I18N.ja[key] || key;
}

function applyI18n() {
  // 言語選択前はHTMLの3言語併記テキストのまま（上書きしない）
  if (!state.lang) return;
  document.documentElement.lang = state.lang;
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n);
  }
  setBanner(state.bannerKey);
}

const BANNER_KEYS = new Set(["idle", "live", "paused", "ended", "disconnected"]);

function setBanner(key) {
  state.bannerKey = key;
  el.banner.className = `banner ${BANNER_KEYS.has(key) ? key : "idle"}`;
  el.banner.textContent = t(`banner_${key}`);
}

// ---- 表示設定（再読み込み後も保持: F-06） ----
// localStorage はプライベートブラウズ等で例外を投げることがあるため保護する
// （初期化が途中で死ぬと設定UIやスクロール追従が無言で壊れる）

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
    /* 保存できなくても表示機能は生かす */
  }
}

function applyFontSize(size) {
  el.captionScreen.classList.remove("size-s", "size-m", "size-l");
  el.captionScreen.classList.add(`size-${size}`);
  for (const btn of el.sizeButtons.children) {
    btn.classList.toggle("selected", btn.dataset.size === size);
  }
  storageSet(STORAGE_FONT_SIZE, size);
}

function applyShowJa(on) {
  el.captionScreen.classList.toggle("hide-ja", !on);
  el.jaToggle.checked = on;
  storageSet(STORAGE_SHOW_JA, on ? "1" : "0");
}

// ---- 自動スクロール追従 ----

function setFollowing(on) {
  state.following = on;
  el.latestBtn.hidden = on;
}

function scrollToLatest() {
  el.cards.scrollTop = el.cards.scrollHeight;
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
      applyI18n(); // 参加画面の案内文・ボタンも選択言語に追従（F-07）
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
    applyI18n();
  });

  applyFontSize(storageGet(STORAGE_FONT_SIZE) || "m");
  applyShowJa(storageGet(STORAGE_SHOW_JA) !== "0");
  el.jaToggle.addEventListener("change", () => applyShowJa(el.jaToggle.checked));
  for (const btn of el.sizeButtons.children) {
    btn.addEventListener("click", () => applyFontSize(btn.dataset.size));
  }

  el.cards.addEventListener("scroll", () => {
    const atBottom =
      el.cards.scrollHeight - el.cards.scrollTop - el.cards.clientHeight < FOLLOW_THRESHOLD_PX;
    setFollowing(atBottom);
  });
  el.latestBtn.addEventListener("click", () => {
    scrollToLatest();
    setFollowing(true);
  });

  applyI18n();
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
      applyI18n();
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
      el.joinError.textContent = t(`err_${msg.reason}`);
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
        applyI18n();
      }
      break;
  }
}

function addCard(msg) {
  if (el.cards.querySelector(`[data-seq="${msg.seq}"]`)) return; // 再送の重複防御
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.seq = msg.seq;
  const text = document.createElement("p");
  text.className = "text";
  text.textContent = msg.text;
  card.append(text);
  if (msg.delay_ms >= DELAY_NOTICE_MS) {
    const tag = document.createElement("span");
    tag.className = "delay-tag";
    tag.dataset.i18n = "delayed"; // 言語変更時に applyI18n で追従させる
    tag.textContent = t("delayed");
    card.append(tag);
  }
  const ja = document.createElement("p");
  ja.className = "ja";
  ja.textContent = msg.ja;
  card.append(ja);
  const time = document.createElement("span");
  time.className = "time";
  time.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  card.append(time);
  // 再接続復元と新着が交錯しても表示は発話順を保つ（seq昇順の位置に挿入）
  let ref = null;
  for (let node = el.cards.lastElementChild; node; node = node.previousElementSibling) {
    if (Number(node.dataset.seq) < msg.seq) break;
    ref = node;
  }
  el.cards.insertBefore(card, ref);
  if (state.following) {
    // 追従中のみ自動スクロール（上スクロールで停止、「最新へ」で復帰）
    scrollToLatest();
  } else if (ref !== null) {
    // 履歴を読んでいる最中に視界より上へ挿入された場合のジャンプ補正
    // （iOS Safari は scroll anchoring 非対応）
    const containerTop = el.cards.getBoundingClientRect().top;
    if (card.getBoundingClientRect().bottom <= containerTop) {
      el.cards.scrollTop += card.offsetHeight + 10; // 10 = カード間のgap
    }
  }
}

init();
