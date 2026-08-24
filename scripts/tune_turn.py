"""発話区切り（turn 連結）の A/B を1コマンドで測る（イシュー#27）。

`scripts/baseline.py`（#24）が改善前の基準線、`scripts/tune_hymt.py`（#26）が
翻訳側の判断材料。こちらは **Segment を Turn へ束ねる戦略の判断材料**を出す。

    python scripts/tune_turn.py                  # 区切りベンチのみ（約20分）
    python scripts/tune_turn.py --repeat 1       # 下見（各条件1周）
    python scripts/tune_turn.py --e2e-ab         # E2E の before/after も撮る（+約10分）
    python scripts/tune_turn.py --report-from docs/bench/2026-08-23-turn-detection.json

比較する条件（すべて whisper small・拡張コーパス `tests/fixtures/ja_ext/`）:

  A  simple        現行。無音 500ms だけで切る（#24 の基準線と同一の数値が出る）
  B  morph 既定     無音長は現行のまま、文法連結だけ足す（実装の既定値）
  C  morph c320     Parapper 実測既定（check 320 / start 96 / force 640）＋ force の掃引
  D  morph nostart  start_speech_ms を切って、その効果だけを分離する

**各条件を周回ラウンドロビンで回す。** ASR のデコード時間は連続実行の発熱と
背景プロセスで簡単に 30% 動く（#26 で E2E を単発比較して読み違えたのと同じ罠）。
同じ条件を連続で回すと先に走った方が有利になるので、周回ごとに一巡させる。

比較指標: 字幕カード数 / 不自然な分割数 / 本当の切れ目の検出 / 平均文字数 /
endpoint latency / **ASR呼び出し回数**（#24 より ASR は固定費が支配的なので、
過分割の削減と引き換えに Segment を増やしていないかを必ず見る）/ Hy-MT2 呼び出し回数。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.bench import run_phase_subprocess, system_info  # noqa: E402
from server.config import load_config  # noqa: E402

# (キー, 表示名, 説明)。キーは scripts/bench.py の PHASES と一致させる
CONDITIONS = [
    ("turn:simple:small", "A simple", "現行。無音500msのみで切る"),
    ("turn:morph:small", "B 既定", "morph: check500 / start96 / force1000（実装の既定）"),
    ("turn:morph-c320:small", "C parapper", "morph: check320 / start96 / force640（Parapper 実測既定）"),
    ("turn:morph-c320-f800:small", "C f800", "morph: check320 / force800"),
    ("turn:morph-c320-f1000:small", "C f1000", "morph: check320 / force1000"),
    ("turn:morph-nostart:small", "D nostart", "morph: check500 / start0 / force1000"),
]
DEFAULT_REPEAT = 2

# E2E の before/after。config.yaml のこの1箇所だけを切り替えて撮る。
# 既定が morph になった（#27）ので、作るのは **before**（#27 以前の挙動）の方
E2E_BEFORE_EDITS = {"strategy: morph": "strategy: simple"}
E2E_MINUTES = 2.0


def aggregate(runs: list[dict]) -> dict:
    """周回の代表値。区切りの判定は決定的なので、ばらつくのは時間の指標だけ。"""
    head = runs[0]
    decode = [r["asr_decode_s_total"] for r in runs if r.get("asr_decode_s_total")]
    for key in ("cards", "unnatural_splits", "asr_calls"):
        assert all(r[key] == head[key] for r in runs), f"{key} が周回で揺れた（決定的でない）"
    return {
        **head,
        "runs": len(runs),
        "asr_decode_s_median": round(statistics.median(decode), 2) if decode else None,
        "asr_decode_s_all": decode,
        # per_clip は生JSONに1周ぶんだけ残す（全周ぶんは読めない量になる）
    }


def load_e2e_runs(work_dir: Path, name: str) -> list[dict]:
    """`<work_dir>/<name>-N/*-acceptance.json` を集める（過去の計測の読み直しにも使う）。"""
    runs = []
    for out in sorted(work_dir.glob(f"{name}-*")):
        raw = next(iter(sorted(out.glob("*-acceptance.json"))), None)
        if raw is None:
            continue
        payload = json.loads(raw.read_text(encoding="utf-8"))
        if payload.get("results"):
            runs.append(payload["results"][0])
    return runs


def morph_label(config) -> str:
    m = config.turn.morph
    return f"morph check{m.check_silence_ms} / start{m.start_speech_ms} / force{m.force_silence_ms}"


def run_e2e_ab(rounds: int, work_dir: Path, after_label: str) -> list[dict] | None:
    """turn.strategy だけを切り替えて E2E を背中合わせで撮る（#26 と同じ手順）。

    単発を過去の記録値と比べるとドリフトを効果として読む（#26 で実際に読んだ）。
    config.yaml の1行だけを交互に書き換え、同じ時間帯に両方を撮る。
    """
    import subprocess

    config_path = ROOT / "config.yaml"
    original = config_path.read_text(encoding="utf-8")
    try:
        for index in range(1, rounds + 1):
            for name in ("before", "after"):
                text = original
                for old, new in (E2E_BEFORE_EDITS if name == "before" else {}).items():
                    if old not in text:
                        print(f"!!! config.yaml に '{old}' が無いので before を作れない")
                        return None
                    text = text.replace(old, new, 1)
                config_path.write_text(text, encoding="utf-8")
                print(f"=== e2e {name} round {index}/{rounds} ===", flush=True)
                out = work_dir / f"{name}-{index}"
                out.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    [
                        sys.executable, str(ROOT / "scripts" / "replay_client.py"),
                        "--minutes", str(E2E_MINUTES),
                        "--engine", "hy-mt2",
                        "--out-dir", str(out),
                        "--no-config-update",
                    ],
                    cwd=str(ROOT),
                )
    finally:
        config_path.write_text(original, encoding="utf-8")  # 必ず戻す
    return [
        {"label": "simple（#27 以前）", "runs": load_e2e_runs(work_dir, "before")},
        {"label": after_label, "runs": load_e2e_runs(work_dir, "after")},
    ]


# ---- レポート ----


def _med(runs: list[dict], pick, digits: int = 2, sign: bool = False) -> str:
    """周回の中央値。値が取れない周回は除く（欠測を0として混ぜない）。"""
    values = [v for v in (pick(r) for r in runs) if v is not None]
    if not values:
        return "-"
    value = round(statistics.median(values), digits)
    if digits == 0:
        value = int(value)
    return f"{value:+}" if sign else str(value)


def _pct_delta(after: float | None, before: float | None) -> str:
    if not before or after is None:
        return "-"
    return f"{(after - before) / before * 100:+.0f}%"


def build_report(info: dict, results: dict, e2e: dict | None) -> str:
    lines: list[str] = []

    def add(line: str = "") -> None:
        lines.append(line)

    add("# 発話区切り: A/B 実測（#27）")
    add()
    add(f"- 機材: {info['cpu']} / {info['cores']}C{info['threads']}T / RAM {info['ram_gb']}GB")
    add(f"- 日時: {info['date']}")
    add("- 音源: `tests/fixtures/ja_ext/`（#22。区切り注釈のある17クリップ / 104.5秒）")
    add("- ASR: faster-whisper small / int8 / CPU")
    add("- 再現: `python scripts/tune_turn.py`")
    add()
    add(
        "数値は合成音声・開発機のもの。**条件間の相対比較にだけ使う**"
        "（絶対値は学校実機でも実音声でも変わる）。"
    )
    add()

    rows = [(label, note, results[key]) for key, label, note in CONDITIONS if results.get(key)]
    if not rows:
        add("計測失敗。")
        return "\n".join(lines) + "\n"
    base = rows[0][2]

    add("## 1. 区切り品質")
    add()
    add("| 条件 | 設定 | カード数 | 不自然な分割 | 本当の切れ目 | 連結された turn | 平均文字数 |")
    add("|------|------|---------|-------------|-------------|----------------|-----------|")
    for label, _note, r in rows:
        setting = (
            "無音500msのみ"
            if r["strategy"] == "simple"
            else f"check{r['min_silence_ms']} / start{r['start_speech_ms']} / force{r['force_silence_ms']}"
        )
        add(
            f"| {label} | {setting} | {r['cards']} | **{r['unnatural_splits']}** | "
            f"{r['natural_breaks_hit']}/{r['natural_breaks_total']} | {r['merged_turns']} | "
            f"{r['card_chars_mean']} |"
        )
    add()
    add(
        "「不自然な分割」= `gaps[].natural_break=false` の位置（または注釈の無い位置）で"
        "割った回数。**これが 0 に近いほど良い**（文中で切らない、が #27 の不変条件）。"
    )
    add()

    add("## 2. 代償（ASR呼び出しと遅延）")
    add()
    add("| 条件 | ASR呼び出し | 対A | decode合計(中央値) | endpoint latency 中央/最大 | Hy-MT2呼び出し |")
    add("|------|-----------|-----|------------------|--------------------------|--------------|")
    for label, _note, r in rows:
        add(
            f"| {label} | {r['asr_calls']} | {_pct_delta(r['asr_calls'], base['asr_calls'])} | "
            f"{r['asr_decode_s_median']}s | "
            f"{r['endpoint_latency_ms_median']}ms / {r['endpoint_latency_ms_max']}ms | "
            f"{r['mt_calls']} |"
        )
    add()
    add(
        "#24 の実測より **ASR は固定費が支配的**（small で `decode ≒ 1.09 + 0.047×audio_s`）。"
        "Segment を増やす条件は、同じ音声でも ASR 時間が増える。"
        f"周回数は {base['runs']} で、decode は中央値。"
    )
    add()

    add("## 3. 条件ごとに何が変わったか")
    add()
    for label, note, r in rows:
        add(f"### {label} — {note}")
        add()
        diffs = []
        for base_clip, clip in zip(base["per_clip"], r["per_clip"]):
            if (base_clip["cards"], base_clip["unnatural_splits"]) != (
                clip["cards"],
                clip["unnatural_splits"],
            ):
                diffs.append((base_clip, clip))
        if not diffs:
            add("A と同じ区切り。")
            add()
            continue
        for base_clip, clip in diffs:
            verdict = "改善" if clip["unnatural_splits"] < base_clip["unnatural_splits"] else "悪化"
            add(
                f"- **{clip['id']}**（{verdict}）: "
                f"{base_clip['cards']}枚/{base_clip['unnatural_splits']}件 → "
                f"{clip['cards']}枚/{clip['unnatural_splits']}件"
            )
            for card in clip["detail"]:
                add(f"    - `{card['reason']}` seg={card['segments']}: {card.get('text', '')}")
        add()

    if e2e:
        add("## 4. E2E（2分リプレイ・擬似生徒10名 / Hy-MT2）")
        add()
        add(
            "| 設定 | 字幕枚数 | first caption | final 中央値 | final p95 | "
            "CPU 中央値 | ASR待ち p95 | RSS増分 |"
        )
        add("|---|---|---|---|---|---|---|---|")
        for dataset in e2e:
            runs = dataset.get("runs") or []
            if not runs:
                continue
            add(
                f"| {dataset['label']} | "
                f"{_med(runs, lambda r: r['captions'], digits=0)} | "
                f"{_med(runs, lambda r: r['first_caption_s'])}s | "
                f"{_med(runs, lambda r: r['latency']['median_s'])}s | "
                f"{_med(runs, lambda r: r['latency']['p95_s'])}s | "
                f"{_med(runs, lambda r: r['cpu_percent']['median'], digits=1)}% | "
                f"{_med(runs, lambda r: (r.get('audio_queue_seconds') or {}).get('p95'))}s | "
                f"{_med(runs, lambda r: r['memory']['increase_mb'], digits=0, sign=True)}MB |"
            )
        add()
        add(
            "リプレイ音源（`tests/fixtures/ja/` の10文）は**1文ずつ独立した完結文**なので、"
            "文法境界の連結はここでは効かない（字幕枚数は変わらない）。"
            "この表で読むのは**連結が効かない音声で代償だけ払っていないか**。"
            "CPU は1コア=100%。`turn.strategy` の1行だけを切り替えて交互に撮っている。"
        )
        add()

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT, help="各条件の周回数（既定2）")
    parser.add_argument("--e2e-ab", action="store_true", help="E2E の before/after も撮る")
    parser.add_argument("--e2e-rounds", type=int, default=2, help="E2E A/B の周回数（既定2）")
    parser.add_argument(
        "--e2e-from",
        action="append",
        default=[],
        metavar="DIR=ラベル",
        help="過去の E2E 計測ディレクトリを追加の比較対象として読む（複数指定可）",
    )
    parser.add_argument("--out-dir", default=str(ROOT / "docs" / "bench"))
    parser.add_argument("--report-from", default=None, help="既存の生JSONからレポートのみ再生成")
    args = parser.parse_args()

    if args.report_from:
        raw = json.loads(Path(args.report_from).read_text(encoding="utf-8"))
        md_path = Path(args.report_from).with_suffix(".md")
        md_path.write_text(
            build_report(raw["system"], raw["results"], raw.get("e2e")), encoding="utf-8"
        )
        print(f"レポート再生成: {md_path}")
        return 0

    config = load_config(ROOT / "config.yaml")
    models_dir = config.models.resolved_dir
    info = system_info()
    print(f"machine: {info['cpu']} / {info['cores']}C{info['threads']}T / {info['ram_gb']}GB")

    rounds: dict[str, list[dict]] = {key: [] for key, _, _ in CONDITIONS}
    for index in range(max(1, args.repeat)):
        print(f"########## round {index + 1}/{args.repeat} ##########", flush=True)
        for key, _label, _note in CONDITIONS:
            result = run_phase_subprocess(key, models_dir)
            if result is not None:
                rounds[key].append(result)

    results = {key: aggregate(runs) for key, runs in rounds.items() if runs}

    e2e: list[dict] = []
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.e2e_ab:
        e2e = run_e2e_ab(args.e2e_rounds, out_dir / "_turn-e2e", morph_label(config)) or []
    for spec in args.e2e_from:
        directory, _, label = spec.partition("=")
        e2e.append({"label": label or directory, "runs": load_e2e_runs(Path(directory), "after")})

    stamp = info["date"]
    json_path = out_dir / f"{stamp}-turn-detection.json"
    json_path.write_text(
        json.dumps(
            {"system": info, "results": results, "e2e": e2e or None},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    md_path = out_dir / f"{stamp}-turn-detection.md"
    md_path.write_text(build_report(info, results, e2e), encoding="utf-8")
    print(f"\n生JSON: {json_path}\nレポート: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
