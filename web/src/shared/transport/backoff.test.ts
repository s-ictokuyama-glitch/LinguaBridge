import { describe, expect, it } from 'vitest';
import { nextPollDelay, type PollTiming } from './backoff';

const timing: PollTiming = { baseMs: 2500, jitterMs: 500, maxMs: 30_000 };
const noJitter = () => 0.5; // (0.5 * 2 - 1) * jitterMs = 0

describe('nextPollDelay', () => {
  it('成功が続いている間は base 間隔を返す', () => {
    expect(nextPollDelay(timing, 0, noJitter)).toBe(2500);
  });

  it('失敗が続くと指数的に伸びる', () => {
    expect(nextPollDelay(timing, 1, noJitter)).toBe(5000);
    expect(nextPollDelay(timing, 2, noJitter)).toBe(10_000);
    expect(nextPollDelay(timing, 3, noJitter)).toBe(20_000);
  });

  it('maxMs で頭打ちになる', () => {
    expect(nextPollDelay(timing, 4, noJitter)).toBe(30_000);
    expect(nextPollDelay(timing, 100, noJitter)).toBe(30_000);
  });

  it('ジッターは ±jitterMs の範囲に収まる', () => {
    expect(nextPollDelay(timing, 0, () => 0)).toBe(2000); // -jitterMs
    expect(nextPollDelay(timing, 0, () => 1)).toBe(3000); // +jitterMs
  });

  it('極端に短い値にはならない（下限 500ms）', () => {
    const tiny: PollTiming = { baseMs: 100, jitterMs: 500, maxMs: 30_000 };
    expect(nextPollDelay(tiny, 0, () => 0)).toBe(500);
  });
});
