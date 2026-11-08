"""ASR の計測記録から採用を判定する（#161）。

使い方は python -m scripts.asr_adoption measurements.json。
CER は割合、E2E 遅延はミリ秒、RSS と配布容量増はバイトで指定する。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from scripts.acceptance import Verdict

MEDIAN_DELAY_LIMIT_MS = 5000
MAX_DELAY_LIMIT_MS = 8000
PEAK_RSS_LIMIT_BYTES = 4_500_000_000
PACKAGE_GROWTH_LIMIT_BYTES = 1_000_000_000
CER_RATIO_LIMIT = Decimal("0.80")


@dataclass(frozen=True)
class CandidateMeasurements:
    cer: Decimal
    median_delay_ms: Decimal
    max_delay_ms: Decimal
    peak_rss_bytes: Decimal
    package_growth_bytes: Decimal


@dataclass(frozen=True)
class AdoptionDecision:
    adopted: str | None
    candidates: dict[str, Verdict]


def judge(baseline_cer: Decimal, candidates: Mapping[str, CandidateMeasurements]) -> AdoptionDecision:
    """足切りと相対 CER 改善を判定し、合格候補の中から CER 最良を選ぶ。"""
    cer_limit = baseline_cer * CER_RATIO_LIMIT
    verdicts: dict[str, Verdict] = {}
    for name, candidate in candidates.items():
        limits = (
            ("遅延中央値", candidate.median_delay_ms, MEDIAN_DELAY_LIMIT_MS, "ms"),
            ("遅延最大", candidate.max_delay_ms, MAX_DELAY_LIMIT_MS, "ms"),
            ("ピーク RSS", candidate.peak_rss_bytes, PEAK_RSS_LIMIT_BYTES, " bytes"),
            ("配布容量増", candidate.package_growth_bytes, PACKAGE_GROWTH_LIMIT_BYTES, " bytes"),
        )
        reasons = [
            f"{label} {measured}{unit} > {limit}{unit}"
            for label, measured, limit, unit in limits
            if measured > limit
        ]
        if not reasons:
            if baseline_cer == 0:
                reasons.append("現行 CER が 0 のため相対 20% 改善できない")
            elif candidate.cer > cer_limit:
                reasons.append(f"CER {candidate.cer} > {cer_limit.normalize()}（相対 20% 改善の上限）")
        verdicts[name] = Verdict(passed=not reasons, reasons=reasons)

    adopted = min(
        (name for name, verdict in verdicts.items() if verdict.passed),
        key=lambda name: (candidates[name].cer, name),
        default=None,
    )
    return AdoptionDecision(adopted=adopted, candidates=verdicts)


def _measurement(value: object, name: str, *, allow_negative: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} は有限の数値で指定してください")
    if value < 0 and not allow_negative:
        raise ValueError(f"{name} は 0 以上で指定してください")
    return value


def evaluate_json(record: str) -> AdoptionDecision:
    """現行 CER と候補名ごとの計測値を含む JSON を判定する。"""
    data = json.loads(record, parse_float=Decimal, parse_int=Decimal, parse_constant=Decimal)
    if not isinstance(data, dict) or not isinstance(data.get("candidates"), dict):
        raise ValueError("候補名をキーにした candidates オブジェクトを指定してください")
    baseline_cer = _measurement(data.get("baseline_cer"), "baseline_cer")
    candidates: dict[str, CandidateMeasurements] = {}
    for name, values in data["candidates"].items():
        if not name.strip() or not isinstance(values, dict):
            raise ValueError("各候補に名前と計測値のオブジェクトを指定してください")
        candidate = CandidateMeasurements(
            cer=_measurement(values.get("cer"), f"{name}.cer"),
            median_delay_ms=_measurement(values.get("median_delay_ms"), f"{name}.median_delay_ms"),
            max_delay_ms=_measurement(values.get("max_delay_ms"), f"{name}.max_delay_ms"),
            peak_rss_bytes=_measurement(values.get("peak_rss_bytes"), f"{name}.peak_rss_bytes"),
            package_growth_bytes=_measurement(
                values.get("package_growth_bytes"), f"{name}.package_growth_bytes", allow_negative=True,
            ),
        )
        if candidate.max_delay_ms < candidate.median_delay_ms:
            raise ValueError(f"{name}.max_delay_ms は median_delay_ms 以上で指定してください")
        candidates[name] = candidate
    return judge(baseline_cer, candidates)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("measurements", type=Path, help="現行 CER と候補ごとの計測値を含む JSON")
    args = parser.parse_args(argv)
    try:
        decision = evaluate_json(args.measurements.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(asdict(decision), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
