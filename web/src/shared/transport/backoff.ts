export type PollTiming = {
  /** 通常時のポーリング間隔 (ms) */
  baseMs: number;
  /** 間隔に加える ± ジッター幅 (ms)。全生徒の同時アクセス集中を避ける */
  jitterMs: number;
  /** 失敗時バックオフの上限 (ms) */
  maxMs: number;
};

export const DEFAULT_POLL_TIMING: PollTiming = {
  baseMs: 2500,
  jitterMs: 500,
  maxMs: 30_000,
};

/**
 * 次のポーリングまでの待ち時間。純関数。
 * 成功が続いている間（consecutiveFailures = 0）は base ± jitter、
 * 失敗が続くと指数的に伸びて maxMs で頭打ちになる。
 */
export function nextPollDelay(
  timing: PollTiming,
  consecutiveFailures: number,
  random: () => number = Math.random,
): number {
  const raw =
    consecutiveFailures > 0
      ? timing.baseMs * 2 ** Math.min(consecutiveFailures, 4)
      : timing.baseMs;
  const capped = Math.min(raw, timing.maxMs);
  const jitter = (random() * 2 - 1) * timing.jitterMs;
  return Math.max(500, Math.round(capped + jitter));
}
