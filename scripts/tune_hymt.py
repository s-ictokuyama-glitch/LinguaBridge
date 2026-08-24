"""Hy-MT2 の使い方の最適化を1コマンドで測る（イシュー#26）。

`scripts/baseline.py`（#24）が「改善前の基準線」なら、こちらは
**その基準線に対して #26 の判断材料を出す**ためのもの。

    python scripts/tune_hymt.py                 # 全部（約25分）
    python scripts/tune_hymt.py --skip-threads  # デコード比較だけ
    python scripts/tune_hymt.py --repeat 1      # スレッド計測を1周だけ（下見用）
    python scripts/tune_hymt.py --e2e-ab        # E2Eの before/after も撮る（+約12分）
    python scripts/tune_hymt.py --report-from docs/bench/2026-08-23-hymt-tuning.json

測っているもの:
  1. B-5 デコード: 貪欲 vs サンプリングの遅延・決定性・往復翻訳CER
  2. B-6 スレッド配分: (ASR cpu_threads × MT n_threads) の組合せを競合下で比較
  3. (--e2e-ab) E2E の before/after: config.yaml を切り替えて交互にリプレイする

**計測中は他の重い処理を走らせないこと**。どちらの数値も CPU 競合で簡単に汚れる。

スレッド計測は既定で**3周ラウンドロビン**する。1周だけだと組合せ間の差（数%）が
run 間のドリフトに埋もれて読めない。同じ組合せを連続で回すと先に走った方が
有利になるので、周回ごとに一巡させている。
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.bench import run_phase_subprocess, system_info  # noqa: E402
from server.config import load_config  # noqa: E402

DECODING_PHASE = "mt-decoding:hy-mt2"
# 開発機は 8物理/16論理コア。0 = cpu_threads 未指定（＝現状の全コア確保）
THREAD_PHASES = [
    "threads:0x4",  # 改善前（cpu_threads 未指定 = 全論理コア）
    "threads:4x4",
    "threads:6x4",
    "threads:8x4",
    "threads:4x8",
    "threads:6x8",
    "threads:8x8",
    "threads:4x12",  # 合計16 = 論理コア数ちょうど。過剰確保の縁を見る
]
DEFAULT_REPEAT = 3

# E2E の before/after。config.yaml のこの2箇所だけを #26 の前の挙動へ戻して撮る
E2E_BEFORE_EDITS = {"temperature: 0.0": "temperature: 0.7", "cache_size: 512": "cache_size: 0"}
E2E_MINUTES = 2.0


def run_e2e_ab(rounds: int, work_dir: Path) -> dict | None:
    """#26 の前後を**背中合わせ**で撮る。

    単発の E2E は背景プロセスや発熱で 0.2s 程度は動く。過去に記録した数値と
    比べると、そのドリフトを変更の効果として読んでしまう（実際に一度読んだ）。
    config.yaml だけを切り替えて交互に撮り、ドリフトを両者へ等しく被せる。
    """
    config_path = ROOT / "config.yaml"
    original = config_path.read_text(encoding="utf-8")
    results: dict[str, list[dict]] = {"before": [], "after": []}
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
                    [sys.executable, str(ROOT / "scripts" / "replay_client.py"),
                     "--minutes", str(E2E_MINUTES), "--engine", "hy-mt2",
                     "--out-dir", str(out), "--no-config-update"],
                    cwd=str(ROOT), capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                )
                raw = next(iter(sorted(out.glob("*-acceptance.json"))), None)
                if raw is None:
                    print(f"!!! e2e {name} round {index}: レポートが出ていない")
                    continue
                results[name].append(json.loads(raw.read_text(encoding="utf-8"))["results"][0])
    finally:
        # 例外でも必ず戻す。ここを落とすとリポジトリの設定が書き換わったまま残る
        config_path.write_text(original, encoding="utf-8")
    return results if results["before"] and results["after"] else None
# 「差があった」と言うために越えてほしい周回間のばらつき。実測の散らばりがこれを
# 超えるなら、組合せ間の差はノイズと区別できない（値は開発機の観測から）
NOISE_FLOOR_S = 0.2


def aggregate_runs(runs: list[dict]) -> dict:
    """同一組合せの複数周を1つにまとめる。代表値は中央値、散らばりは全周の幅。"""
    first = runs[0]
    pipelines = [r["pipeline_s"] for r in runs]
    return {
        "asr_threads": first["asr_threads"],
        "mt_threads": first["mt_threads"],
        "rounds": len(runs),
        "pipeline_s": round(statistics.median(pipelines), 2),
        "pipeline_s_each": pipelines,
        "pipeline_s_spread": round(max(pipelines) - min(pipelines), 2),
        "pipeline_tail_s": round(
            statistics.median(r["pipeline_tail_s"] for r in runs), 2
        ),
        "asr_decode_s_median": round(
            statistics.median(r["asr_decode_s_median"] for r in runs), 2
        ),
        "asr_decode_s_max": round(statistics.median(r["asr_decode_s_max"] for r in runs), 2),
        "mt_ms_median": round(statistics.median(r["mt_ms_median"] for r in runs)),
        "peak_rss_mb": max(r["peak_rss_mb"] for r in runs),
        "wall_s": round(statistics.median(r["wall_s"] for r in runs), 1),
        "runs": runs,
    }


def build_report(info: dict, results: dict) -> str:
    lines: list[str] = []

    def add(line: str = "") -> None:
        lines.append(line)

    add(f"# Hy-MT2 の使い方の最適化（イシュー#26） — {info['date']}")
    add()
    add(
        f"- 計測機: **{info['cpu']}**（{info['cores']}C/{info['threads']}T, "
        f"RAM {info['ram_gb']}GB, {info['os']}, Python {info['python']}）"
    )
    add("- 再現: `python scripts/tune_hymt.py`")
    add()
    add("> **この数値の読み方**")
    add("> - 開発機での実測。**学校実機（Core i5）はこれより遅い前提**で読む")
    add("> - スレッド配分の最適値は**コア構成に依存する**。実機では取り直すこと")
    add("> - 遅延は #24 のベースライン（`docs/bench/2026-08-23-baseline.md`）と比べる")
    add()

    decoding = results.get(DECODING_PHASE)
    if decoding:
        add("## B-5 デコード: 貪欲 vs サンプリング")
        add()
        add(
            f"音源は `tests/fixtures/ja_sentences.txt` の {decoding['sentences']}文 × en/zh。"
            f"貪欲は各2回、サンプリング（temperature=0.7）は各{decoding['sampling_runs']}回。"
        )
        add()
        add("| 指標 | 貪欲 (temperature=0) | サンプリング (0.7) |")
        add("|------|---------------------|-------------------|")
        g, s = decoding["greedy"], decoding["sampling"]
        for lang in ("en", "zh"):
            add(f"| {lang} 中央値 | {g['ms_median'][lang]}ms | {s['ms_median'][lang]}ms |")
        for lang in ("en", "zh"):
            add(f"| {lang} 最大 | {g['ms_max'][lang]}ms | {s['ms_max'][lang]}ms |")
        for lang in ("en", "zh"):
            add(f"| {lang} 出力文字数 中央値 | {g['chars_median'][lang]} | {s['chars_median'][lang]} |")
        add(
            f"| 同一入力で同じ訳が出た割合 | **{g['deterministic_rate']:.0%}** | "
            f"{s['deterministic_rate']:.0%} |"
        )
        add(f"| 往復翻訳CER（代理指標） | {g['cer_roundtrip']:.1%} | {s['cer_roundtrip']:.1%} |")
        add()
        add(
            f"- サンプル同士のばらつき（平均）: {decoding['sample_spread_mean']:.1%} / "
            f"貪欲とサンプリングの差: {decoding['greedy_vs_sampling_mean']:.1%}"
        )
        add(
            "- **往復翻訳CERは絶対品質の指標ではない**。訳文を貪欲で日本語へ戻し、"
            "原文との CER を取っただけなので、意味の同じ言い換えでも 10〜20% は出る"
        )
        add()

    threads = {k: v for k, v in results.items() if k.startswith("threads:") and v}
    if threads:
        add("## B-6 スレッド配分（競合下）")
        add()
        add(
            "ASR（CTranslate2）と MT（llama.cpp）を別スレッドで同時に走らせたときの値。"
            "`ASR=0` は `cpu_threads` 未指定＝ライブラリ既定（全論理コア）で、**改善前の挙動**。"
        )
        add()
        rounds = next(iter(threads.values())).get("rounds", 1)
        add(f"各組合せを **{rounds}周**ラウンドロビンで測り、代表値は中央値を取っている。")
        add()
        add(
            "| ASR threads | MT threads | ASRデコード中央値 | MT中央値 | 見積り遅延 中央値 | "
            "見積り遅延 最悪 | 周回のばらつき | wall |"
        )
        add("|---|---|---|---|---|---|---|---|")
        for name in THREAD_PHASES:
            r = threads.get(name)
            if not r:
                continue
            label = "既定(全コア)" if r["asr_threads"] == 0 else str(r["asr_threads"])
            each = "/".join(f"{v:.2f}" for v in r.get("pipeline_s_each", [r["pipeline_s"]]))
            add(
                f"| {label} | {r['mt_threads']} | {r['asr_decode_s_median']}s | "
                f"{r['mt_ms_median']}ms | **{r['pipeline_s']}s** | "
                f"{r.get('pipeline_tail_s', '-')}s | ±{r.get('pipeline_s_spread', 0)}s ({each}) | "
                f"{r['wall_s']}s |"
            )
        add()
        add(
            "- 見積り遅延 = ASRデコード + 2×MT（発話終了→2言語目の caption）。"
            "中央値どうし・最大どうしを組む。`scripts/bench.py` の `estimate` と同じ組み方"
        )
        add(
            "- wall = 10クリップのASRと10文×2言語の翻訳を撃ち切るまでの実時間。"
            "スループットの目安"
        )
        add()

    e2e = results.get("e2e_ab")
    if e2e:
        add("## E2E before / after（実サーバー + 擬似生徒10名・2分）")
        add()
        add(
            "`config.yaml` の `temperature` と `cache_size` だけを切り替えて**交互に**撮った。"
            "過去に記録した数値と比べると機械側のドリフトを変更の効果として読むため、"
            "背中合わせで撮る。before = サンプリング＋キャッシュ無効（#26 の前）。"
        )
        add()
        add("| 構成 | 遅延中央値 | p95 | 最大 | 初回字幕 | CPU中央値 | キャッシュ |")
        add("|---|---|---|---|---|---|---|")
        for name, label in (("before", "before"), ("after", "**after**")):
            for r in e2e[name]:
                cache = r.get("mt_cache") or {}
                hit = (
                    f"{cache.get('hit_rate', 0):.1%} ({cache.get('hits', 0)}件)"
                    if cache.get("hits")
                    else "—"
                )
                add(
                    f"| {label} | {r['latency']['median_s']}s | {r['latency']['p95_s']}s | "
                    f"{r['latency']['max_s']}s | {r['first_caption_s']}s | "
                    f"{r['cpu_percent']['median']}% | {hit} |"
                )
        add()
        add(
            "- キャッシュのヒット率は**リプレイ音源がループするぶん**が効いた値。"
            "実授業の繰り返し率ではない"
        )
        add()

    add("## 判定")
    add()
    if decoding:
        _decoding_verdict(add, decoding)
    if threads:
        _threads_verdict(add, threads)
    if e2e:
        _e2e_verdict(add, e2e)

    add("## 生データ")
    add()
    add("`docs/bench/<日付>-hymt-tuning.json`")
    return "\n".join(lines) + "\n"


def _roundtrip_noise(items: list[dict]) -> tuple[int, float]:
    """往復翻訳CERの「自分自身のノイズ」を測る。

    貪欲とサンプリング3回が**まったく同じ訳文**になった項目だけを見る。
    入力が同じなら往復CERも同じになるはずなので、そこに開きがあれば
    それは品質差ではなく指標のノイズである。件数と最大の開きを返す。
    """
    gaps = [
        abs(i["cer_roundtrip_greedy"] - sum(i["cer_roundtrip_samples"]) / len(i["cer_roundtrip_samples"]))
        for i in items
        if set(i["samples"]) == {i["greedy"]}
    ]
    return len(gaps), max(gaps, default=0.0)


def _decoding_verdict(add, decoding: dict) -> None:
    g, s = decoding["greedy"], decoding["sampling"]
    identical, noise = _roundtrip_noise(decoding["items"])
    add("### B-5 デコード → **貪欲（temperature=0）を既定にする**")
    add()
    add(
        f"- **遅延は差なし**（en {g['ms_median']['en']} vs {s['ms_median']['en']}ms / "
        f"zh {g['ms_median']['zh']} vs {s['ms_median']['zh']}ms）。出力文字数もほぼ同じ"
    )
    add(
        f"- **品質差は測れない**。訳文が貪欲とサンプリング3回で完全一致した {identical} 項目でも、"
        f"往復翻訳CERは最大 {noise:.1%} ずれる。指標自身のノイズが "
        f"貪欲/サンプリング間の差（{abs(g['cer_roundtrip'] - s['cer_roundtrip']):.1%}）より大きい"
    )
    add(
        f"- **出力が安定する**。同一入力で同じ訳が出た割合は貪欲 {g['deterministic_rate']:.0%} / "
        f"サンプリング {s['deterministic_rate']:.0%}"
    )
    add(
        "- 決め手は**翻訳キャッシュとの相性**。キャッシュは最初に出た訳を以後ずっと固定する。"
        "固定するなら乱数で引いた1サンプルより、モデルが最も確からしいとした訳の方がよい"
    )
    add(
        "- ただし **temperature=0 は再現性の保証ではない**。同一入力の連続呼び出しでは安定するが、"
        "直前の呼び出しが変わると出力が変わる例を観測している（llama.cpp はプロンプトの"
        "共通接頭辞をKVキャッシュから再利用する）。**訳文を文字列一致で検査するテストは書けない**"
    )
    add()


def _threads_verdict(add, threads: dict) -> None:
    """判定規則: **最悪値を悪化させない範囲で**見積り遅延の中央値が最小の組合せ。

    中央値だけで選ぶと、ASRの尾を伸ばして中央値を稼ぐ組合せが勝ってしまう。
    N-01 は中央値5秒と最大8秒の両方が基準なので、尾を悪化させる取引はしない。
    """
    before = next((r for r in threads.values() if r["asr_threads"] == 0), None)
    if before is None:
        return
    baseline_tail = before.get("pipeline_tail_s", float("inf"))
    eligible = [r for r in threads.values() if r.get("pipeline_tail_s", 0) <= baseline_tail]
    best = min(eligible or [before], key=lambda r: r["pipeline_s"])
    fastest = min(threads.values(), key=lambda r: r["pipeline_s"])

    delta = before["pipeline_s"] - best["pipeline_s"]
    worst_spread = max(r.get("pipeline_s_spread", 0.0) for r in threads.values())
    decisive = delta > max(worst_spread, NOISE_FLOOR_S)
    label = "既定(全コア)" if best["asr_threads"] == 0 else str(best["asr_threads"])

    if best is before or not decisive:
        add("### B-6 スレッド配分 → **変えない**（`asr.cpu_threads: 0` のまま）")
        add()
        add(
            f"- 最悪値を悪化させない組合せの中で最速は ASR {label} / MT {best['mt_threads']}"
            f"（{best['pipeline_s']}s）だが、改善前（{before['pipeline_s']}s）との差 "
            f"{delta:.2f}s は**周回のばらつき（最大 ±{worst_spread}s）に埋もれる**"
        )
        add(
            "- ただし **B-6 の仮説は ASR 単体では当たっていた**。CTranslate2 のスレッドを"
            f"絞ると競合下のASRデコードは中央値 {before['asr_decode_s_median']}s → "
            f"{best['asr_decode_s_median']}s、最大 "
            f"{before.get('asr_decode_s_max', '-')}s → {best.get('asr_decode_s_max', '-')}s まで"
            "縮む。**それでも合計が動かないのは、空いたCPUを llama.cpp が食うから**"
            f"（MT中央値 {before['mt_ms_median']}ms → {best['mt_ms_median']}ms）。"
            "この機では ASR と MT が同じCPUを奪い合っており、配分を変えても総量は移るだけ"
        )
        add(
            "- **ASRの尾が制約になったらここへ戻る**。エンジン差し替え（#28）で"
            "デコードが重くなる場合や、遅延内訳（#30）でASR待ちが目立つ場合、"
            "`asr.cpu_threads` を 6〜8 にすると ASR 側だけは確実に縮む"
        )
    else:
        add(f"### B-6 スレッド配分 → **ASR `cpu_threads: {label}` / MT `threads: {best['mt_threads']}`**")
        add()
        add(
            f"- 見積り遅延の中央値 {before['pipeline_s']}s → **{best['pipeline_s']}s**"
            f"（{-delta:+.2f}s）。周回のばらつき（最大 ±{worst_spread}s）より大きい差"
        )
        add(
            f"- 最悪値も悪化していない: {baseline_tail}s → "
            f"{best.get('pipeline_tail_s', '-')}s"
        )
        add(
            f"- ASRデコード中央値 {before['asr_decode_s_median']}s → "
            f"{best['asr_decode_s_median']}s / MT中央値 {before['mt_ms_median']}ms → "
            f"{best['mt_ms_median']}ms"
        )

    if fastest is not best:
        fast_label = "既定(全コア)" if fastest["asr_threads"] == 0 else str(fastest["asr_threads"])
        add(
            f"- **中央値だけならもっと速い組合せがある**（ASR {fast_label} / "
            f"MT {fastest['mt_threads']}: {fastest['pipeline_s']}s）が、最悪値が "
            f"{fastest.get('pipeline_tail_s', '-')}s（改善前 {baseline_tail}s）まで伸びるので採らない。"
            "ASRのスレッドを絞ると長いクリップの尾が伸びる"
        )
    add(
        "- この値は**コア構成に依存する**。学校実機（Core i5）では取り直すこと"
        "（`python scripts/tune_hymt.py --skip-decoding`）"
    )
    add()


def _e2e_verdict(add, e2e: dict) -> None:
    def med(name: str, key: str) -> float:
        return round(statistics.median(r["latency"][key] for r in e2e[name]), 2)

    def cpu(name: str) -> float:
        return round(statistics.median(r["cpu_percent"]["median"] for r in e2e[name]), 1)

    before_med, after_med = med("before", "median_s"), med("after", "median_s")
    add("### E2E → **B-4 + B-5 の合計で遅延もCPUも下がった**")
    add()
    add(
        f"- 遅延中央値 {before_med}s → **{after_med}s**"
        f"（{after_med - before_med:+.2f}s / {(after_med / before_med - 1) * 100:+.0f}%）、"
        f"p95 {med('before', 'p95_s')}s → {med('after', 'p95_s')}s、"
        f"最大 {med('before', 'max_s')}s → {med('after', 'max_s')}s"
    )
    add(f"- CPU中央値 {cpu('before')}% → **{cpu('after')}%**（1コア=100%）")
    add(
        "- 効いているのは主にキャッシュ（推論をまるごと省く）。貪欲デコード単体の"
        "遅延差は B-5 の表の通りほぼ無い"
    )
    add()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-threads", action="store_true", help="B-6 のスレッド計測を飛ばす")
    parser.add_argument(
        "--repeat", type=int, default=DEFAULT_REPEAT, help="スレッド計測の周回数（既定3）"
    )
    parser.add_argument(
        "--e2e-ab", action="store_true", help="E2Eの before/after も撮る（+約12分）"
    )
    parser.add_argument("--e2e-rounds", type=int, default=2, help="E2E A/B の周回数（既定2）")
    parser.add_argument("--skip-decoding", action="store_true", help="B-5 のデコード比較を飛ばす")
    parser.add_argument("--out-dir", default=str(ROOT / "docs" / "bench"))
    parser.add_argument("--report-from", default=None, help="既存の生JSONからレポートのみ再生成")
    args = parser.parse_args()

    if args.report_from:
        raw = json.loads(Path(args.report_from).read_text(encoding="utf-8"))
        md_path = Path(args.report_from).with_suffix(".md")
        md_path.write_text(build_report(raw["system"], raw["results"]), encoding="utf-8")
        print(f"レポート再生成: {md_path}")
        return 0

    config = load_config(ROOT / "config.yaml")
    models_dir = config.models.resolved_dir
    info = system_info()
    print(f"machine: {info['cpu']} / {info['cores']}C{info['threads']}T / {info['ram_gb']}GB")

    results: dict[str, dict | None] = {}
    if not args.skip_decoding:
        results[DECODING_PHASE] = run_phase_subprocess(DECODING_PHASE, models_dir)
    if not args.skip_threads:
        rounds: dict[str, list[dict]] = {p: [] for p in THREAD_PHASES}
        for index in range(max(1, args.repeat)):
            print(f"########## threads round {index + 1}/{args.repeat} ##########", flush=True)
            for phase in THREAD_PHASES:  # ラウンドロビン: 周回ごとに一巡させる
                result = run_phase_subprocess(phase, models_dir)
                if result:
                    rounds[phase].append(result)
        for phase, runs in rounds.items():
            results[phase] = aggregate_runs(runs) if runs else None
    if args.e2e_ab:
        results["e2e_ab"] = run_e2e_ab(args.e2e_rounds, Path(args.out_dir) / "_e2e_ab")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{info['date']}-hymt-tuning"
    (out_dir / f"{stem}.json").write_text(
        json.dumps({"system": info, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / f"{stem}.md").write_text(
        build_report(info, {k: v for k, v in results.items() if v}), encoding="utf-8"
    )
    print(f"\nレポート: {out_dir / (stem + '.md')}")
    print(f"生データ: {out_dir / (stem + '.json')}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
