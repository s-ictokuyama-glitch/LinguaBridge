import { describe, expect, it } from 'vitest';
import { InMemoryRelayHub, InMemoryTransport } from './InMemoryTransport';
import type { Segment, TransportStatus } from '../types';

const seg = (seq: number): Segment => ({ seq, text: `文${seq}`, tMs: 1000 + seq });

describe('InMemoryTransport (先生→生徒の一気通貫)', () => {
  it('先生が publish した文が参加中の生徒に届く', async () => {
    const hub = new InMemoryRelayHub();
    const teacher = new InMemoryTransport(hub);
    const student = new InMemoryTransport(hub);

    const { roomId } = await teacher.createRoom();
    const received: Segment[] = [];
    student.onSegments((batch) => received.push(...batch));
    student.join(roomId, 0);

    await teacher.publish([seg(1)]);
    await teacher.publish([seg(2)]);

    expect(received.map((s) => s.text)).toEqual(['文1', '文2']);
  });

  it('途中参加の生徒は sinceSeq より後の履歴だけ受け取る', async () => {
    const hub = new InMemoryRelayHub();
    const teacher = new InMemoryTransport(hub);
    const { roomId } = await teacher.createRoom();
    await teacher.publish([seg(1), seg(2), seg(3)]);

    const late = new InMemoryTransport(hub);
    const received: Segment[] = [];
    late.onSegments((batch) => received.push(...batch));
    late.join(roomId, 2);

    expect(received.map((s) => s.seq)).toEqual([3]);
  });

  it('先生の再送（同じ seq）は生徒側で重複しない', async () => {
    const hub = new InMemoryRelayHub();
    const teacher = new InMemoryTransport(hub);
    const student = new InMemoryTransport(hub);
    const { roomId } = await teacher.createRoom();
    const received: Segment[] = [];
    student.onSegments((batch) => received.push(...batch));
    student.join(roomId, 0);

    await teacher.publish([seg(1), seg(2)]);
    await teacher.publish([seg(1), seg(2), seg(3)]); // ネットワーク断からの再送を想定

    expect(received.map((s) => s.seq)).toEqual([1, 2, 3]);
  });

  it('リングバッファ溢れ後の新規参加は欠落として通知される', async () => {
    const hub = new InMemoryRelayHub(2); // バッファ2文
    const teacher = new InMemoryTransport(hub);
    const { roomId } = await teacher.createRoom();
    await teacher.publish([seg(1), seg(2), seg(3), seg(4)]);

    const student = new InMemoryTransport(hub);
    const received: Segment[] = [];
    let gapAt: number | null = null;
    student.onSegments((batch) => received.push(...batch));
    student.onGap((firstAvailable) => (gapAt = firstAvailable));
    student.join(roomId, 0);

    expect(received.map((s) => s.seq)).toEqual([3, 4]);
    expect(gapAt).toBe(3);
  });

  it('ルーム終了で生徒に closed が通知される', async () => {
    const hub = new InMemoryRelayHub();
    const teacher = new InMemoryTransport(hub);
    const student = new InMemoryTransport(hub);
    const { roomId } = await teacher.createRoom();
    const statuses: TransportStatus[] = [];
    student.onStatus((s) => statuses.push(s));
    student.join(roomId, 0);

    await teacher.closeRoom();

    expect(statuses).toEqual(['connected', 'closed']);
  });
});
