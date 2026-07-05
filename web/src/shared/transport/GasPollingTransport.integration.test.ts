import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { GasPollingTransport } from './GasPollingTransport';
import { InMemoryRelayHub } from './InMemoryTransport';
import type { Segment } from '../types';

/**
 * GAS 中継 (relay-gas/Code.gs) と同じ API 契約をローカル HTTP サーバーで再現し、
 * GasPollingTransport を実際の fetch / タイマーで動かす統合テスト。
 * 意味論（seq 冪等・差分ポーリング）は InMemoryRelayHub を流用する。
 */

let server: Server;
let baseUrl: string;
let hub: InMemoryRelayHub;

beforeAll(async () => {
  hub = new InMemoryRelayHub();
  server = createServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://localhost');
    const action = url.searchParams.get('action');
    const respond = (obj: unknown) => {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify(obj));
    };

    if (req.method === 'GET' && action === 'poll') {
      respond(
        hub.poll(url.searchParams.get('roomId') ?? '', Number(url.searchParams.get('sinceSeq') ?? 0)),
      );
      return;
    }

    let raw = '';
    req.on('data', (chunk) => (raw += chunk));
    req.on('end', () => {
      try {
        type PostBody = { roomId?: string; teacherToken?: string; segments?: Segment[] };
        const body: PostBody = raw ? (JSON.parse(raw) as PostBody) : {};
        if (action === 'create') {
          respond(hub.createRoom());
        } else if (action === 'publish') {
          hub.publish(body.roomId ?? '', body.teacherToken ?? '', body.segments ?? []);
          respond({ ok: true });
        } else if (action === 'close') {
          hub.closeRoom(body.roomId ?? '', body.teacherToken ?? '');
          respond({ ok: true });
        } else {
          respond({ error: 'unknown_action' });
        }
      } catch (err) {
        respond({ error: String(err) });
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(0, resolve));
  const { port } = server.address() as AddressInfo;
  baseUrl = `http://127.0.0.1:${port}/exec`;
});

afterAll(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

const fastPolling = { timing: { baseMs: 50, jitterMs: 0, maxMs: 1000 } };

describe('GasPollingTransport (実HTTP統合)', () => {
  it('先生の publish が生徒のポーリングで届き、再送しても重複しない', async () => {
    const teacher = new GasPollingTransport(baseUrl);
    const student = new GasPollingTransport(baseUrl, fastPolling);

    const { roomId } = await teacher.createRoom();
    const received: Segment[] = [];
    student.onSegments((batch) => received.push(...batch));
    student.join(roomId, 0);

    await teacher.publish([{ seq: 1, text: 'こんにちは', tMs: Date.now() }]);
    await teacher.publish([
      { seq: 1, text: 'こんにちは', tMs: Date.now() }, // ネットワーク断からの再送を想定
      { seq: 2, text: '今日は天気の話をします', tMs: Date.now() },
    ]);

    await vi.waitFor(
      () => expect(received.map((s) => s.text)).toEqual(['こんにちは', '今日は天気の話をします']),
      { timeout: 3000 },
    );
    student.close();
  });

  it('ルーム終了が生徒に closed として伝わる', async () => {
    const teacher = new GasPollingTransport(baseUrl);
    const student = new GasPollingTransport(baseUrl, fastPolling);

    const { roomId } = await teacher.createRoom();
    let lastStatus = '';
    student.onStatus((s) => (lastStatus = s));
    student.join(roomId, 0);

    await vi.waitFor(() => expect(lastStatus).toBe('connected'), { timeout: 3000 });
    await teacher.closeRoom();
    await vi.waitFor(() => expect(lastStatus).toBe('closed'), { timeout: 3000 });
    student.close();
  });

  it('存在しないルームへの参加は closed と表示される', async () => {
    const student = new GasPollingTransport(baseUrl, fastPolling);
    let lastStatus = '';
    student.onStatus((s) => (lastStatus = s));
    student.join('NOROOM', 0);

    await vi.waitFor(() => expect(lastStatus).toBe('closed'), { timeout: 3000 });
    student.close();
  });
});
