import { GasPollingTransport } from '../shared/transport/GasPollingTransport';
import { getRelayOverride, getRelayUrl } from '../shared/config';
import type { Segment, TransportStatus } from '../shared/types';

const $ = <T extends HTMLElement>(id: string): T => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`element not found: #${id}`);
  return el as T;
};

const statusBanner = $('status-banner');
const joinSection = $('join');
const feed = $('feed');
const roomInput = $<HTMLInputElement>('room-input');
const joinBtn = $<HTMLButtonElement>('join-btn');

const params = new URLSearchParams(location.search);
const roomId = (params.get('room') ?? '').trim().toUpperCase();
const debugLatency = params.has('debug');

if (!roomId) {
  showJoinForm();
} else {
  start(roomId);
}

function showJoinForm(): void {
  joinSection.classList.remove('hidden');
  const join = () => {
    const code = roomInput.value.trim().toUpperCase();
    if (!code) return;
    const url = new URL(location.href);
    url.searchParams.set('room', code);
    const override = getRelayOverride();
    if (override) url.searchParams.set('relay', override);
    location.href = url.toString();
  };
  joinBtn.addEventListener('click', join);
  roomInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) join();
  });
  roomInput.focus();
}

function start(room: string): void {
  const relayUrl = getRelayUrl();
  if (!relayUrl) {
    showBanner('中継 URL が未設定です。先生に確認してください。', 'closed');
    return;
  }

  feed.classList.remove('hidden');

  const transport = new GasPollingTransport(relayUrl);

  transport.onSegments((batch) => {
    for (const seg of batch) appendLine(seg);
    feed.lastElementChild?.scrollIntoView({ block: 'end' });
  });

  transport.onGap(() => {
    const notice = document.createElement('p');
    notice.className = 'gap-notice';
    notice.textContent = 'これ以前の履歴は表示できません';
    feed.append(notice);
  });

  transport.onStatus((status: TransportStatus) => {
    if (status === 'connected') {
      hideBanner();
    } else if (status === 'reconnecting') {
      showBanner('再接続中…', 'reconnecting');
    } else {
      showBanner('ルームは終了しました（またはまだ開始されていません）', 'closed');
    }
  });

  transport.join(room, 0);
}

function appendLine(segment: Segment): void {
  const line = document.createElement('p');
  line.className = 'line';

  const text = document.createElement('span');
  text.textContent = segment.text;
  line.append(text);

  // 発話確定→表示の遅延。S2 スパイクの計測に使う（?debug で画面表示、常時 console に記録）
  const latencyMs = Date.now() - segment.tMs;
  console.info(`[LinguaBridge] seq=${segment.seq} latency=${latencyMs}ms`);
  if (debugLatency) {
    const badge = document.createElement('span');
    badge.className = 'debug-latency';
    badge.textContent = `+${(latencyMs / 1000).toFixed(1)}s`;
    line.append(badge);
  }

  const time = document.createElement('time');
  time.textContent = new Date(segment.tMs).toLocaleTimeString('ja-JP');
  line.append(time);

  feed.append(line);
}

function showBanner(message: string, className: 'reconnecting' | 'closed'): void {
  statusBanner.textContent = message;
  statusBanner.className = `status-banner ${className}`;
}

function hideBanner(): void {
  statusBanner.className = 'status-banner hidden';
}
