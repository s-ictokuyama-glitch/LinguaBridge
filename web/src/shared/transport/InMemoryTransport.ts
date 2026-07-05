import type { CreateRoomResponse, Segment, TransportStatus } from '../types';
import type { RelayTransport } from './RelayTransport';
import { acceptSegments } from './segmentStream';

type Room = {
  teacherToken: string;
  segments: Segment[];
  latestSeq: number;
  active: boolean;
  listeners: Set<() => void>;
};

/**
 * テスト用のインメモリ中継サーバー。GAS 中継と同じ意味論
 * （seq 冪等な追記・リングバッファ・差分取得）をプロセス内で再現する。
 */
export class InMemoryRelayHub {
  private rooms = new Map<string, Room>();
  private nextRoomNumber = 1;

  constructor(private readonly maxLog = 200) {}

  createRoom(): CreateRoomResponse {
    const roomId = `ROOM${this.nextRoomNumber++}`;
    const teacherToken = `token-${roomId}`;
    this.rooms.set(roomId, {
      teacherToken,
      segments: [],
      latestSeq: 0,
      active: true,
      listeners: new Set(),
    });
    return { roomId, teacherToken };
  }

  publish(roomId: string, teacherToken: string, segments: Segment[]): void {
    const room = this.mustGetRoom(roomId);
    if (room.teacherToken !== teacherToken) throw new Error('invalid teacherToken');
    if (!room.active) throw new Error('room is closed');

    const sorted = [...segments].sort((a, b) => a.seq - b.seq);
    for (const seg of sorted) {
      if (seg.seq <= room.latestSeq) continue; // 再送は冪等に無視
      room.segments.push(seg);
      room.latestSeq = seg.seq;
    }
    if (room.segments.length > this.maxLog) {
      room.segments = room.segments.slice(room.segments.length - this.maxLog);
    }
    for (const notify of room.listeners) notify();
  }

  poll(roomId: string, sinceSeq: number): { segments: Segment[]; latestSeq: number; active: boolean } {
    const room = this.rooms.get(roomId);
    if (!room) return { segments: [], latestSeq: sinceSeq, active: false };
    return {
      segments: room.segments.filter((s) => s.seq > sinceSeq),
      latestSeq: room.latestSeq,
      active: room.active,
    };
  }

  closeRoom(roomId: string, teacherToken: string): void {
    const room = this.mustGetRoom(roomId);
    if (room.teacherToken !== teacherToken) throw new Error('invalid teacherToken');
    room.active = false;
    for (const notify of room.listeners) notify();
  }

  subscribe(roomId: string, notify: () => void): () => void {
    const room = this.mustGetRoom(roomId);
    room.listeners.add(notify);
    return () => room.listeners.delete(notify);
  }

  private mustGetRoom(roomId: string): Room {
    const room = this.rooms.get(roomId);
    if (!room) throw new Error(`room not found: ${roomId}`);
    return room;
  }
}

/** InMemoryRelayHub を介した RelayTransport のフェイク実装（プッシュ配信） */
export class InMemoryTransport implements RelayTransport {
  private roomId = '';
  private teacherToken = '';
  private lastSeq = 0;
  private lastStatus: TransportStatus | null = null;
  private unsubscribe: (() => void) | null = null;

  private segmentCbs: Array<(batch: Segment[]) => void> = [];
  private gapCbs: Array<(firstAvailableSeq: number) => void> = [];
  private statusCbs: Array<(status: TransportStatus) => void> = [];

  constructor(private readonly hub: InMemoryRelayHub) {}

  async createRoom(): Promise<CreateRoomResponse> {
    const res = this.hub.createRoom();
    this.roomId = res.roomId;
    this.teacherToken = res.teacherToken;
    return res;
  }

  async publish(segments: Segment[]): Promise<void> {
    this.hub.publish(this.roomId, this.teacherToken, segments);
  }

  async closeRoom(): Promise<void> {
    this.hub.closeRoom(this.roomId, this.teacherToken);
  }

  join(roomId: string, sinceSeq: number): void {
    this.roomId = roomId;
    this.lastSeq = sinceSeq;
    this.unsubscribe = this.hub.subscribe(roomId, () => this.pull());
    this.pull();
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
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  private pull(): void {
    const data = this.hub.poll(this.roomId, this.lastSeq);
    const { accepted, newLastSeq, gapBeforeSeq } = acceptSegments(this.lastSeq, data.segments);
    this.lastSeq = newLastSeq;
    if (gapBeforeSeq !== null) for (const cb of this.gapCbs) cb(gapBeforeSeq);
    if (accepted.length > 0) for (const cb of this.segmentCbs) cb(accepted);
    this.emitStatus(data.active ? 'connected' : 'closed');
  }

  private emitStatus(status: TransportStatus): void {
    if (status === this.lastStatus) return;
    this.lastStatus = status;
    for (const cb of this.statusCbs) cb(status);
  }
}
