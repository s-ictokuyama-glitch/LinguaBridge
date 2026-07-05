import QRCode from 'qrcode';
import { GasPollingTransport } from '../shared/transport/GasPollingTransport';
import { getRelayOverride, getRelayUrl } from '../shared/config';
import type { Segment } from '../shared/types';

const $ = <T extends HTMLElement>(id: string): T => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`element not found: #${id}`);
  return el as T;
};

const relayWarning = $('relay-warning');
const setupSection = $('setup');
const roomSection = $('room');
const composeSection = $('compose');
const createRoomBtn = $<HTMLButtonElement>('create-room');
const closeRoomBtn = $<HTMLButtonElement>('close-room');
const roomCodeEl = $('room-code');
const qrCanvas = $<HTMLCanvasElement>('qr');
const studentUrlEl = $('student-url');
const textInput = $<HTMLInputElement>('text-input');
const sendBtn = $<HTMLButtonElement>('send');
const sendStatus = $('send-status');
const sentLog = $<HTMLUListElement>('sent-log');

const relayUrl = getRelayUrl();
if (!relayUrl) {
  relayWarning.classList.remove('hidden');
  createRoomBtn.disabled = true;
}

const transport = new GasPollingTransport(relayUrl);

let nextSeq = 1;
const pending: Segment[] = [];
let flushing = false;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
const logItems = new Map<number, HTMLLIElement>();

createRoomBtn.addEventListener('click', () => void createRoom());
closeRoomBtn.addEventListener('click', () => void closeRoom());
sendBtn.addEventListener('click', () => send());
textInput.addEventListener('keydown', (e) => {
  // IME の変換確定 Enter では送信しない
  if (e.key === 'Enter' && !e.isComposing) send();
});

async function createRoom(): Promise<void> {
  createRoomBtn.disabled = true;
  createRoomBtn.textContent = '作成中…';
  try {
    const { roomId } = await transport.createRoom();

    const studentUrl = new URL('student.html', location.href);
    studentUrl.searchParams.set('room', roomId);
    const override = getRelayOverride();
    if (override) studentUrl.searchParams.set('relay', override);

    roomCodeEl.textContent = roomId;
    studentUrlEl.textContent = studentUrl.toString();
    await QRCode.toCanvas(qrCanvas, studentUrl.toString(), { width: 220, margin: 1 });

    setupSection.classList.add('hidden');
    roomSection.classList.remove('hidden');
    composeSection.classList.remove('hidden');
    textInput.focus();
  } catch (err) {
    createRoomBtn.disabled = false;
    createRoomBtn.textContent = 'ルームを作成';
    alert(`ルームを作成できませんでした: ${String(err)}`);
  }
}

async function closeRoom(): Promise<void> {
  if (!confirm('ルームを終了しますか？生徒側には終了と表示されます。')) return;
  closeRoomBtn.disabled = true;
  try {
    await transport.closeRoom();
    composeSection.classList.add('hidden');
    setSendStatus('ルームを終了しました。', '');
  } catch (err) {
    closeRoomBtn.disabled = false;
    alert(`終了に失敗しました: ${String(err)}`);
  }
}

function send(): void {
  const text = textInput.value.trim();
  if (!text) return;
  textInput.value = '';

  const segment: Segment = { seq: nextSeq++, text, tMs: Date.now() };
  pending.push(segment);
  appendLogItem(segment);
  void flush();
}

/**
 * 未送信キューをまとめて配信する。失敗しても文は捨てず、
 * 3秒後に同じ seq で再送する（中継側で冪等に処理される）。
 */
async function flush(): Promise<void> {
  if (flushing || pending.length === 0) return;
  flushing = true;
  setSendStatus('送信中…', '');
  try {
    const batch = pending.slice();
    await transport.publish(batch);
    pending.splice(0, batch.length);
    for (const seg of batch) markLogItem(seg.seq, 'sent', '配信済み');
    setSendStatus('配信済み', 'ok');
  } catch {
    for (const seg of pending) markLogItem(seg.seq, 'failed', '再送待ち');
    setSendStatus('送信に失敗しました。自動で再送します…', 'error');
    if (retryTimer === null) {
      retryTimer = setTimeout(() => {
        retryTimer = null;
        void flush();
      }, 3000);
    }
  } finally {
    flushing = false;
    if (pending.length > 0 && retryTimer === null) void flush();
  }
}

function appendLogItem(segment: Segment): void {
  const li = document.createElement('li');
  li.className = 'pending';
  const state = document.createElement('span');
  state.className = 'state';
  state.textContent = '送信中';
  const text = document.createElement('span');
  text.textContent = segment.text;
  li.append(state, text);
  logItems.set(segment.seq, li);
  sentLog.prepend(li);
}

function markLogItem(seq: number, className: 'sent' | 'failed', label: string): void {
  const li = logItems.get(seq);
  if (!li) return;
  li.className = className;
  const state = li.querySelector('.state');
  if (state) state.textContent = label;
}

function setSendStatus(message: string, className: '' | 'ok' | 'error'): void {
  sendStatus.textContent = message;
  sendStatus.className = className;
}
