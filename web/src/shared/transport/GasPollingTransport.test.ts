import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GasPollingTransport } from './GasPollingTransport';
import type { Segment, TransportStatus } from '../types';

const RELAY = 'https://relay.example/exec';
const seg = (seq: number): Segment => ({ seq, text: `文${seq}`, tMs: 1000 + seq });

const jsonResponse = (body: unknown): Response =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });

const makeTransport = (fetchFn: typeof fetch) =>
  new GasPollingTransport(RELAY, {
    fetchFn,
    timing: { baseMs: 2500, jitterMs: 0, maxMs: 30_000 },
    random: () => 0.5,
  });

describe('GasPollingTransport', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('createRoom は text/plain の POST でプリフライトを回避し、roomId とトークンを得る', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ roomId: 'ABC123', teacherToken: 'tok-1' }));
    const t = makeTransport(fetchMock);

    const res = await t.createRoom();

    expect(res).toEqual({ roomId: 'ABC123', teacherToken: 'tok-1' });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(`${RELAY}?action=create`);
    expect(init.method).toBe('POST');
    expect(init.headers['Content-Type']).toContain('text/plain');
  });

  it('publish はルーム情報とトークンを添えて文を送る', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ roomId: 'ABC123', teacherToken: 'tok-1' }))
      .mockResolvedValueOnce(jsonResponse({ ok: true, latestSeq: 1 }));
    const t = makeTransport(fetchMock);
    await t.createRoom();

    await t.publish([seg(1)]);

    const [url, init] = fetchMock.mock.calls[1]!;
    expect(url).toBe(`${RELAY}?action=publish`);
    expect(JSON.parse(init.body)).toEqual({
      roomId: 'ABC123',
      teacherToken: 'tok-1',
      segments: [seg(1)],
    });
  });

  it('サーバーが {error} を返したら例外にする', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ error: 'room_not_found' }));
    const t = makeTransport(fetchMock);
    await expect(t.createRoom()).rejects.toThrow('room_not_found');
  });

  it('join 後、差分ポーリングで受信し、次回の sinceSeq が進む', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ segments: [seg(1), seg(2)], latestSeq: 2, active: true }))
      .mockResolvedValueOnce(jsonResponse({ segments: [seg(2), seg(3)], latestSeq: 3, active: true }));
    const t = makeTransport(fetchMock);
    const received: Segment[] = [];
    t.onSegments((batch) => received.push(...batch));

    t.join('ABC123', 0);
    await vi.advanceTimersByTimeAsync(0); // 初回ポーリング

    expect(received.map((s) => s.seq)).toEqual([1, 2]);
    expect(fetchMock.mock.calls[0]![0]).toContain('sinceSeq=0');

    await vi.advanceTimersByTimeAsync(2500); // 2回目: サーバーが seg2 を重複再送してきても
    expect(received.map((s) => s.seq)).toEqual([1, 2, 3]); // 重複排除される
    expect(fetchMock.mock.calls[1]![0]).toContain('sinceSeq=2');

    t.close();
  });

  it('失敗すると reconnecting になり、間隔が指数的に伸び、復帰で戻る', async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(jsonResponse({ segments: [], latestSeq: 0, active: true }));
    const t = makeTransport(fetchMock);
    const statuses: TransportStatus[] = [];
    t.onStatus((s) => statuses.push(s));

    t.join('ABC123', 0);
    await vi.advanceTimersByTimeAsync(0); // 初回: 失敗
    expect(statuses).toEqual(['reconnecting']);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // バックオフ中 (failures=1 → 5000ms)。base の 2500ms ではまだ再試行しない
    await vi.advanceTimersByTimeAsync(2500);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(2500); // 合計 5000ms で再試行 → 成功
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(statuses).toEqual(['reconnecting', 'connected']);

    t.close();
  });

  it('ルーム終了 (active:false) で closed を通知しポーリングを止める', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ segments: [], latestSeq: 0, active: false }));
    const t = makeTransport(fetchMock);
    const statuses: TransportStatus[] = [];
    t.onStatus((s) => statuses.push(s));

    t.join('ABC123', 0);
    await vi.advanceTimersByTimeAsync(0);
    expect(statuses).toEqual(['closed']);

    await vi.advanceTimersByTimeAsync(60_000); // その後は一切ポーリングしない
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('close() 後はポーリングが止まる', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ segments: [], latestSeq: 0, active: true }));
    const t = makeTransport(fetchMock);

    t.join('ABC123', 0);
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    t.close();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
