/**
 * LinguaBridge 中継サーバー (Google Apps Script Web アプリ)
 *
 * デプロイ手順:
 *   1. script.google.com で新しいプロジェクトを作成し、このファイルの内容を貼り付ける
 *   2. デプロイ > 新しいデプロイ > 種類: ウェブアプリ
 *      - 実行ユーザー: 自分
 *      - アクセスできるユーザー: 全員
 *   3. 発行された /exec URL を web/src/shared/config.ts の DEFAULT_RELAY_URL に設定する
 *
 * API (すべて JSON を返す):
 *   POST ?action=create                      -> { roomId, teacherToken }
 *   POST ?action=publish  body: { roomId, teacherToken, segments: [{seq,text,tMs}] }
 *                                            -> { ok: true, latestSeq }
 *   POST ?action=close    body: { roomId, teacherToken }
 *                                            -> { ok: true }
 *   GET  ?action=poll&roomId=X&sinceSeq=N    -> { segments, latestSeq, active }
 *
 * POST のボディは text/plain の JSON（ブラウザ側の CORS プリフライト回避のため）。
 */

var CACHE_TTL_SEC = 21600; // 6時間。授業1日分あれば十分
var MAX_LOG = 200; // 途中参加者に見せる直近文数（リングバッファ）
var MAX_LOG_JSON_CHARS = 90000; // CacheService の 100KB/キー制限への安全マージン
var MAX_TEXT_LENGTH = 500; // 1文の最大文字数
var ROOM_CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'; // 紛らわしい文字 (I,L,O,0,1) を除外
var ROOM_CODE_LENGTH = 6;

function doGet(e) {
  return handleRequest_(e, null);
}

function doPost(e) {
  var body = null;
  try {
    body = e.postData && e.postData.contents ? JSON.parse(e.postData.contents) : {};
  } catch (err) {
    return jsonOutput_({ error: 'invalid_json' });
  }
  return handleRequest_(e, body);
}

function handleRequest_(e, body) {
  try {
    var action = (e.parameter && e.parameter.action) || '';
    if (action === 'create') return jsonOutput_(createRoom_());
    if (action === 'publish') return jsonOutput_(publish_(body || {}));
    if (action === 'close') return jsonOutput_(closeRoom_(body || {}));
    if (action === 'poll') {
      return jsonOutput_(poll_(String(e.parameter.roomId || ''), Number(e.parameter.sinceSeq || 0)));
    }
    return jsonOutput_({ error: 'unknown_action' });
  } catch (err) {
    return jsonOutput_({ error: String(err) });
  }
}

function jsonOutput_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(
    ContentService.MimeType.JSON,
  );
}

// ---- actions ----

function createRoom_() {
  var cache = CacheService.getScriptCache();
  var roomId = '';
  for (var attempt = 0; attempt < 5; attempt++) {
    var candidate = randomRoomCode_();
    if (!cache.get(metaKey_(candidate))) {
      roomId = candidate;
      break;
    }
  }
  if (!roomId) return { error: 'room_id_exhausted' };

  var teacherToken = Utilities.getUuid();
  var meta = { token: teacherToken, latestSeq: 0, active: true, createdAt: Date.now() };
  cache.put(metaKey_(roomId), JSON.stringify(meta), CACHE_TTL_SEC);
  cache.put(logKey_(roomId), JSON.stringify([]), CACHE_TTL_SEC);
  return { roomId: roomId, teacherToken: teacherToken };
}

function publish_(body) {
  if (!body.roomId || !body.teacherToken || !Array.isArray(body.segments)) {
    return { error: 'bad_request' };
  }
  var lock = LockService.getScriptLock();
  lock.waitLock(5000);
  try {
    var cache = CacheService.getScriptCache();
    var meta = readJson_(cache, metaKey_(body.roomId));
    if (!meta) return { error: 'room_not_found' };
    if (meta.token !== body.teacherToken) return { error: 'invalid_token' };
    if (!meta.active) return { error: 'room_closed' };

    var log = readJson_(cache, logKey_(body.roomId)) || [];
    var segments = body.segments.slice().sort(function (a, b) {
      return Number(a.seq) - Number(b.seq);
    });

    for (var i = 0; i < segments.length; i++) {
      var seg = segments[i];
      var seq = Number(seg.seq);
      // 再送 (seq <= latestSeq) は冪等に無視する
      if (!isFinite(seq) || seq <= meta.latestSeq) continue;
      log.push({
        seq: seq,
        text: String(seg.text || '').slice(0, MAX_TEXT_LENGTH),
        tMs: Number(seg.tMs) || Date.now(),
      });
      meta.latestSeq = seq;
    }

    // リングバッファ: 件数と JSON サイズの両方で直近分だけ残す
    if (log.length > MAX_LOG) log = log.slice(log.length - MAX_LOG);
    while (log.length > 1 && JSON.stringify(log).length > MAX_LOG_JSON_CHARS) {
      log.shift();
    }

    cache.put(metaKey_(body.roomId), JSON.stringify(meta), CACHE_TTL_SEC);
    cache.put(logKey_(body.roomId), JSON.stringify(log), CACHE_TTL_SEC);
    return { ok: true, latestSeq: meta.latestSeq };
  } finally {
    lock.releaseLock();
  }
}

function poll_(roomId, sinceSeq) {
  var cache = CacheService.getScriptCache();
  var meta = readJson_(cache, metaKey_(roomId));
  if (!meta) {
    // 存在しない/期限切れのルームは「終了」として返す（生徒側で明示表示）
    return { segments: [], latestSeq: sinceSeq, active: false };
  }
  var log = readJson_(cache, logKey_(roomId)) || [];
  var segments = [];
  for (var i = 0; i < log.length; i++) {
    if (log[i].seq > sinceSeq) segments.push(log[i]);
  }
  return { segments: segments, latestSeq: meta.latestSeq, active: !!meta.active };
}

function closeRoom_(body) {
  if (!body.roomId || !body.teacherToken) return { error: 'bad_request' };
  var lock = LockService.getScriptLock();
  lock.waitLock(5000);
  try {
    var cache = CacheService.getScriptCache();
    var meta = readJson_(cache, metaKey_(body.roomId));
    if (!meta) return { error: 'room_not_found' };
    if (meta.token !== body.teacherToken) return { error: 'invalid_token' };
    meta.active = false;
    cache.put(metaKey_(body.roomId), JSON.stringify(meta), CACHE_TTL_SEC);
    return { ok: true };
  } finally {
    lock.releaseLock();
  }
}

// ---- helpers ----

function metaKey_(roomId) {
  return 'room:' + roomId + ':meta';
}

function logKey_(roomId) {
  return 'room:' + roomId + ':log';
}

function readJson_(cache, key) {
  var raw = cache.get(key);
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch (err) {
    return null;
  }
}

function randomRoomCode_() {
  var code = '';
  for (var i = 0; i < ROOM_CODE_LENGTH; i++) {
    code += ROOM_CODE_ALPHABET.charAt(Math.floor(Math.random() * ROOM_CODE_ALPHABET.length));
  }
  return code;
}
