import type { CreateRoomResponse, PollResponse, Segment, TransportStatus } from '../types';
import type { RelayTransport } from './RelayTransport';
import { acceptSegments } from './segmentStream';
import { DEFAULT_POLL_TIMING, nextPollDelay, type PollTiming } from './backoff';

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export type GasPollingTransportOptions = {
  timing?: PollTiming;
  /** テスト用: fetch の差し替え */
  fetchFn?: FetchLike;
  /** テスト用: 乱数の差し替え（ジッター制御） */
  random?: () => number;
};

/**
 * GAS Web アプリを中継とする RelayTransport 実装。
 * GAS は WebSocket を張れないため、生徒側は sinceSeq 差分の HTTP ポーリングで受信する。
 * POST は text/plain ボディにして CORS プリフライトを回避する（GAS の既知の制約）。
 */
export class GasPollingTransport implements RelayTransport {
  private readonly timing: PollTiming;
  private readonly fetchFn: FetchLike;
  private readonly random: () => number;

  private roomId = '';
  private teacherToken = '';
  private lastSeq = 0;
  private consecutiveFailures = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  private lastStatus: TransportStatus | null = null;

  private segmentCbs: Array<(batch: Segment[]) => void> = [];
  private gapCbs: Array<(firstAvailableSeq: number) => void> = [];
  private statusCbs: Array<(status: TransportStatus) => void> = [];

  constructor(
    private readonly relayUrl: string,
    options: GasPollingTransportOptions = {},
  ) {
    this.timing = options.timing ?? DEFAULT_POLL_TIMING;
    this.fetchFn = options.fetchFn ?? ((input, init) => fetch(input, init));
    this.random = options.random ?? Math.random;
  }

  async createRoom(): Promise<CreateRoomResponse> {
    const res = await this.post<CreateRoomResponse>('create', {});
    this.roomId = res.roomId;
    this.teacherToken = res.teacherToken;
    return res;
  }

  async publish(segments: Segment[]): Promise<void> {
    await this.post('publish', {
      roomId: this.roomId,
      teacherToken: this.teacherToken,
      segments,
    });
  }

  async closeRoom(): Promise<void> {
    await this.post('close', {
      roomId: this.roomId,
      teacherToken: this.teacherToken,
    });
  }

  join(roomId: string, sinceSeq: number): void {
    this.roomId = roomId;
    this.lastSeq = sinceSeq;
    this.stopped = false;
    void this.pollOnce();
  }

  onSegments(cb: (batch: Segment[]) => void): void {
    this.segmentCbs.push(cb);
  }

  onGap(cb: (firstAvailableSeq: number) => void): void {
    this.gapCbs.push(cb);
  }

  onStatus(cb: (status: TransportStatus) => void): void {
    this.statusCbs.push(cb);
  }

  close(): void {
    this.stopped = true;
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private async pollOnce(): Promise<void> {
    if (this.stopped) return;

    let roomActive = true;
    try {
      const url =
        `${this.relayUrl}?action=poll` +
        `&roomId=${encodeURIComponent(this.roomId)}` +
        `&sinceSeq=${this.lastSeq}`;
      const resp = await this.fetchFn(url, { method: 'GET' });
      if (!resp.ok) throw new Error(`poll failed: HTTP ${resp.status}`);
      const data = (await resp.json()) as PollResponse & { error?: string };
      if (data.error) throw new Error(data.error);

      const { accepted, newLastSeq, gapBeforeSeq } = acceptSegments(this.lastSeq, data.segments);
      this.lastSeq = newLastSeq;
      if (gapBeforeSeq !== null) for (const cb of this.gapCbs) cb(gapBeforeSeq);
      if (accepted.length > 0) for (const cb of this.segmentCbs) cb(accepted);

      this.consecutiveFailures = 0;
      roomActive = data.active;
      this.emitStatus(roomActive ? 'connected' : 'closed');
    } catch {
      this.consecutiveFailures += 1;
      this.emitStatus('reconnecting');
    }

    if (this.stopped || !roomActive) return;
    const delay = nextPollDelay(this.timing, this.consecutiveFailures, this.random);
    this.timer = setTimeout(() => void this.pollOnce(), delay);
  }

  private emitStatus(status: TransportStatus): void {
    if (status === this.lastStatus) return;
    this.lastStatus = status;
    for (const cb of this.statusCbs) cb(status);
  }

  private async post<T>(action: string, body: unknown): Promise<T> {
    const resp = await this.fetchFn(`${this.relayUrl}?action=${encodeURIComponent(action)}`, {
      method: 'POST',
      // text/plain にすることで CORS プリフライト (OPTIONS) を発生させない
      headers: { 'Content-Type': 'text/plain;charset=utf-8' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`${action} failed: HTTP ${resp.status}`);
    const data = (await resp.json()) as T & { error?: string };
    if (data && typeof data === 'object' && 'error' in data && data.error) {
      throw new Error(String(data.error));
    }
    return data;
  }
}
