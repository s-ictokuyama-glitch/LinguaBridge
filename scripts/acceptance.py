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

# 遅延ドリフト（#32・N-08「メモリ増加傾向なし」の遅延版）。
# 長時間試験で見たいのは「遅いか」ではなく「だんだん遅くなるか」で、
# 平均や p95 では出ない（前半の速さに薄められる）ので時間窓で並べる
DRIFT_WINDOW_S = 300.0  # 5分窓
DRIFT_REL_LIMIT = 0.50  # 最後の窓が最初の窓より +50%
DRIFT_ABS_LIMIT_S = 1.0  # かつ +1.0s 遅ければ「悪化傾向あり」

# リソースリーク（#32）。スレッド数・ハンドル数・asyncioタスク数は
# 定常運転では一定のはずで、単調に増えるならリークを疑う
RESOURCE_REL_LIMIT = 0.25  # +25%
RESOURCE_ABS_LIMIT = 20.0  # かつ +20（本・個）


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


def _halves(values: list[float], warmup_frac: float) -> tuple[float, float] | None:
    """warmup を捨てた残りを前半/後半に割り、それぞれの中央値を返す。

    リークの判定はどの指標でも「前半の平常値と後半の平常値を比べる」であって、
    最初と最後の1点を比べることではない（1点はGCや瞬間的な山に振られる）。
    """
    if len(values) < 4:
        return None
    start = int(len(values) * warmup_frac)  # 起動直後のロード変動を除外（len>=4 で start<len）
    usable = values[start:]
    half = len(usable) // 2
    return statistics.median(usable[:half]), statistics.median(usable[half:])


def memory_trend(rss_mb: list[float], warmup_frac: float = 0.1) -> MemoryTrend | None:
    """RSS推移の前半/後半の中央値を比べ、リーク傾向とピークを判定する。"""
    halves = _halves(rss_mb, warmup_frac)
    if halves is None:
        return None
    baseline, final = halves
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
class DriftWindow:
    """1つの時間窓に入った caption の遅延。"""

    index: int  # 0 起点の窓番号
    start_s: float  # 窓の開始（配信開始からの経過秒）
    count: int
    median_s: float
    max_s: float


@dataclass
class LatencyDrift:
    windows: list[DriftWindow]  # caption が1件以上あった窓だけ
    empty_windows: int  # 期間内で caption が1件も無かった窓の数
    first_median_s: float
    last_median_s: float
    increase_s: float
    drifting: bool  # 試験中に遅延が悪化したか


def latency_drift(
    samples: list[tuple[float, int]], window_s: float = DRIFT_WINDOW_S
) -> LatencyDrift | None:
    """`(経過秒, delay_ms)` を時間窓へ束ね、窓ごとの中央値の推移を出す（#32）。

    `memory_trend` が前半/後半の2分割なのに対し、遅延は「いつから悪化したか」を
    見たいので窓を並べる。判定に使うのは最初と最後の窓だけで、間の窓は
    レポートで人が読むためのもの。

    窓が1つしか作れない（＝推移が取れない）なら None。
    caption が1件も無かった窓は中央値に混ぜず、`empty_windows` として数える
    （0件の窓を0秒として平均すると「速くなった」に見えてしまう）。
    """
    if not samples:
        return None
    buckets: dict[int, list[int]] = {}
    for at_s, delay_ms in samples:
        buckets.setdefault(int(at_s // window_s), []).append(delay_ms)
    last_index = max(buckets)
    if last_index == 0:
        return None  # 窓が1つ＝推移が存在しない
    windows = [
        DriftWindow(
            index=i,
            start_s=i * window_s,
            count=len(buckets[i]),
            median_s=round(statistics.median(buckets[i]) / 1000, 2),
            max_s=round(max(buckets[i]) / 1000, 2),
        )
        for i in sorted(buckets)
    ]
    first, last = windows[0].median_s, windows[-1].median_s
    increase = round(last - first, 2)
    return LatencyDrift(
        windows=windows,
        empty_windows=(last_index + 1) - len(windows),
        first_median_s=first,
        last_median_s=last,
        increase_s=increase,
        drifting=increase > DRIFT_ABS_LIMIT_S and increase > first * DRIFT_REL_LIMIT,
    )


@dataclass
class ResourceTrend:
    """スレッド数・ハンドル数・asyncioタスク数の推移（#32）。"""

    name: str
    samples: int
    baseline: float  # warmup後・前半の中央値
    final: float  # 後半の中央値
    peak: float
    increase: float
    growing: bool  # リーク傾向ありか


def resource_trend(
    name: str, values: list[float], warmup_frac: float = 0.1
) -> ResourceTrend | None:
    """リソース数の前半/後半の中央値を比べ、リーク傾向を判定する（#32）。

    メモリと同じ数え方に揃える理由は、これらのリークがメモリ増加としては
    現れないことがあるため（ハンドルとタスクは1個あたりのRSSが小さく、
    5GBの N-05 に当たる前に ulimit / ハンドル上限の側で先に壊れる）。
    """
    halves = _halves(values, warmup_frac)
    if halves is None:
        return None
    baseline, final = halves
    increase = final - baseline
    return ResourceTrend(
        name=name,
        samples=len(values),
        baseline=round(baseline, 1),
        final=round(final, 1),
        peak=round(max(values), 1),
        increase=round(increase, 1),
        growing=increase > RESOURCE_ABS_LIMIT and increase > baseline * RESOURCE_REL_LIMIT,
    )


@dataclass
class DroppedAudio:
    """試験中に失われた音声（#32）。破棄地点は2つあり、意味が違う。

    - `segments` / `seconds`: **容量**側。`limits.asr_queue_seconds` に達して
      捨てた確定 Segment。0 でなければ授業で字幕が欠けている。
    - `frames_rejected`: **不正入力**側。`limits.max_audio_bytes` を超えたフレーム。
      正常なクライアントでは 0 で、0 でなければ壊れた送信元がいる。

    まとめて1つの型にしているのは、この3つが judge・Results・レポートの
    どこへ行くにも必ず一緒に動くため。
    """

    segments: int = 0
    seconds: float = 0.0
    frames_rejected: int = 0

    @property
    def any_loss(self) -> bool:
        return self.segments > 0 or self.frames_rejected > 0


@dataclass
class RestoreResult:
    """切断注入下の差分復元の結果（#35）。

    再接続の復元が効いているかは**生徒ごとに**「自分の言語で配信された seq を
    全部持っているか」でしか見えない。`(seq, lang)` で重複排除した集合では
    「誰かが受け取った」しか分からず、切断された当人が取り戻せたかは分からない。
    """

    students: int
    gaps: int  # 履歴の**内側**なのに届いていない seq の総数（＝復元の失敗）
    beyond_history: int  # 履歴上限の外で届かなかった seq の総数（＝仕様どおり）
    worst_student: str | None  # gaps が最も多かった生徒
    worst_gaps: int

    @property
    def complete(self) -> bool:
        return self.gaps == 0


def restore_gaps(
    student_seqs: dict[str, set[int]],
    student_langs: dict[str, str],
    published_by_lang: dict[str, set[int]],
    history_limit: int = 50,
) -> RestoreResult | None:
    """生徒ごとの seq 到達を、その言語で配信された seq と突き合わせる（#35）。

    `history_limit` は `config.yaml` の `history_resend`。**これより古い欠落は
    仕様どおり**で、`joined.history_from` が「これより前は恒久欠落」を
    クライアントへ伝える契約になっている。ここを失敗に数えると、長い切断を
    注入するほど落ちる＝判定の意味が反転する。

    **近似していることを明示しておく**: サーバーの復元は再接続した**その時点**の
    `last_seq` と履歴上限で決まるが、ここでは試験終了時の「最新 `history_limit` 件」を
    復元対象とみなしている。切断ごとの窓を追う代わりの割り切りで、判定としては
    **保守的な側**に倒れている: 試験は全生徒が接続した状態で drain して終わるので、
    最新 `history_limit` 件のどれかが最後まで欠けていれば、それは確実に復元の失敗。
    それより古い欠落は、正当な履歴切れと復元失敗を区別できないので数えない。
    """
    if not student_seqs:
        return None
    total_gaps = 0
    total_beyond = 0
    worst: tuple[str, int] | None = None
    for sid, got in student_seqs.items():
        published = published_by_lang.get(student_langs.get(sid, ""), set())
        if not published:
            continue
        # 復元対象は「最新 history_limit 件」。それより古いものは範囲外
        recoverable = set(sorted(published)[-history_limit:])
        gaps = len(recoverable - got)
        total_gaps += gaps
        total_beyond += len((published - recoverable) - got)
        if worst is None or gaps > worst[1]:
            worst = (sid, gaps)
    return RestoreResult(
        students=len(student_seqs),
        gaps=total_gaps,
        beyond_history=total_beyond,
        worst_student=worst[0] if worst else None,
        worst_gaps=worst[1] if worst else 0,
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
    drift: LatencyDrift | None = None,
    resources: list[ResourceTrend | None] | None = None,
    dropped: DroppedAudio | None = None,
    restore: RestoreResult | None = None,
) -> Verdict:
    """計測値を受け入れ基準に照らし、合否と不合格理由を返す。

    `drift` / `resources` / `dropped` は長時間試験の追加判定（#32）、
    `restore` は切断注入下の復元判定（#35）で、
    省略すれば #17 の判定と完全に同じになる（既存の呼び出しは不変）。
    いずれも N-08「試験長を通して劣化なし」の別の顔なので N-08 として数える。
    """
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
    if drift is not None and drift.drifting:
        reasons.append(
            f"遅延のドリフトあり: 最初の窓 {drift.first_median_s}s → "
            f"最後の窓 {drift.last_median_s}s (+{drift.increase_s}s) (N-08)"
        )
    for res in resources or []:
        if res is not None and res.growing:
            reasons.append(
                f"{res.name} が増加傾向 ({res.baseline:g}→{res.final:g}, "
                f"+{res.increase:g})、リーク疑い (N-08)"
            )
    if restore is not None and not restore.complete:
        reasons.append(
            f"切断後に復元されなかった字幕が {restore.gaps} 件"
            f"（最悪の生徒 {restore.worst_student}: {restore.worst_gaps}件） (N-08)"
        )
    if dropped is not None and dropped.any_loss:
        if dropped.segments > 0:
            reasons.append(
                f"音声の破棄 {dropped.segments}件 ({dropped.seconds:.1f}s)"
                f"、処理が追いつかなかった (N-08)"
            )
        if dropped.frames_rejected > 0:
            reasons.append(
                f"上限超過の音声フレーム {dropped.frames_rejected}枚を受け取れなかった (N-08)"
            )
    if ran_seconds < target_seconds * 0.98:
        reasons.append(f"試験が最後まで完走しなかった ({ran_seconds:.0f}s / {target_seconds:.0f}s)")
    return Verdict(passed=not reasons, reasons=reasons)
