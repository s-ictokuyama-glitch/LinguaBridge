"""性能受け入れ試験の判定ロジック（イシュー#17）。

replay_client.py が収集した計測値を PRD の受け入れ基準（N-01/N-05/N-08）に
照らして合否判定する純粋関数群。実I/Oを持たないのでユニットテスト可能。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

# 受け入れ基準（PRD）
LATENCY_MEDIAN_LIMIT_S = 5.0  # N-01: 発話終了→表示 中央値
LATENCY_MAX_LIMIT_S = 8.0  # N-01: 最大
RSS_LIMIT_MB = 5000  # N-05: 常駐メモリ ≤ 5GB
# メモリ増加傾向（リーク）判定: 後半中央値が前半中央値を
# 相対・絶対の両方の閾値を超えて上回ったら「増加傾向あり」とする（N-08）
LEAK_REL_LIMIT = 0.10  # +10%
LEAK_ABS_LIMIT_MB = 300  # かつ +300MB


@dataclass
class LatencyStats:
    count: int
    median_s: float
    p95_s: float
    max_s: float


def latency_stats(delays_ms: list[int]) -> LatencyStats | None:
    """caption の delay_ms（発話終了→送出）分布から中央値・p95・最大を出す。"""
    if not delays_ms:
        return None
    ordered = sorted(delays_ms)
    idx95 = min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))
    return LatencyStats(
        count=len(ordered),
        median_s=round(statistics.median(ordered) / 1000, 2),
        p95_s=round(ordered[idx95] / 1000, 2),
        max_s=round(ordered[-1] / 1000, 2),
    )


@dataclass
class MemoryTrend:
    samples: int
    baseline_mb: int  # warmup後・前半の中央値
    final_mb: int  # 後半の中央値
    peak_mb: int
    increase_mb: int
    increasing: bool  # リーク傾向ありか


def memory_trend(rss_mb: list[float], warmup_frac: float = 0.1) -> MemoryTrend | None:
    """RSS推移の前半/後半の中央値を比べ、リーク傾向とピークを判定する。"""
    if len(rss_mb) < 4:
        return None
    start = int(len(rss_mb) * warmup_frac)  # 起動直後のロード変動を除外（len>=4 で start<len）
    usable = rss_mb[start:]
    half = len(usable) // 2
    baseline = statistics.median(usable[:half])
    final = statistics.median(usable[half:])
    increase = final - baseline
    increasing = increase > LEAK_ABS_LIMIT_MB and increase > baseline * LEAK_REL_LIMIT
    return MemoryTrend(
        samples=len(rss_mb),
        baseline_mb=round(baseline),
        final_mb=round(final),
        peak_mb=round(max(rss_mb)),
        increase_mb=round(increase),
        increasing=increasing,
    )


@dataclass
class Verdict:
    passed: bool
    reasons: list[str]  # 不合格理由（空なら合格）


def judge(
    latency: LatencyStats | None,
    memory: MemoryTrend | None,
    *,
    crashes: int,
    reconnect_failures: int,
    ran_seconds: float,
    target_seconds: float,
) -> Verdict:
    """計測値を受け入れ基準に照らし、合否と不合格理由を返す。"""
    reasons: list[str] = []
    if latency is None:
        reasons.append("captionを1件も受信できなかった（ASR/翻訳が機能していない可能性）")
    else:
        if latency.median_s > LATENCY_MEDIAN_LIMIT_S:
            reasons.append(f"遅延中央値 {latency.median_s}s > {LATENCY_MEDIAN_LIMIT_S}s (N-01)")
        if latency.max_s > LATENCY_MAX_LIMIT_S:
            reasons.append(f"遅延最大 {latency.max_s}s > {LATENCY_MAX_LIMIT_S}s (N-01)")
    if memory is None:
        reasons.append("メモリ計測サンプルが不足")
    else:
        if memory.peak_mb > RSS_LIMIT_MB:
            reasons.append(f"常駐メモリ {memory.peak_mb}MB > {RSS_LIMIT_MB}MB (N-05)")
        if memory.increasing:
            reasons.append(f"メモリ増加傾向あり (+{memory.increase_mb}MB)、リーク疑い (N-08)")
    if crashes > 0:
        reasons.append(f"サーバークラッシュ {crashes}回 (N-08)")
    if reconnect_failures > 0:
        reasons.append(f"切断復元失敗 {reconnect_failures}回 (N-08)")
    if ran_seconds < target_seconds * 0.98:
        reasons.append(f"試験が最後まで完走しなかった ({ran_seconds:.0f}s / {target_seconds:.0f}s)")
    return Verdict(passed=not reasons, reasons=reasons)
