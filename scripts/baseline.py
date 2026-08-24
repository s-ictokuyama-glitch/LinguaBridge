"""改善前のベースライン計測（イシュー#24）。

#25〜#30 のすべてが「改善したか / 悪化していないか」をこの数値と比べて判定する。
1コマンドで ASR精度・MT遅延・競合下の劣化・区切り品質・E2E を測り、
`docs/bench/<日付>-baseline.md` + `.json` に残す。

    python scripts/baseline.py                  # 全部（約15〜20分）
    python scripts/baseline.py --minutes 5      # E2E を5分に伸ばす
    python scripts/baseline.py --skip-e2e       # ベンチだけ（サーバーを起動しない）
    python scripts/baseline.py --report-from docs/bench/2026-08-23-baseline.json

測っているもの:
  1. ASR: 拡張コーパス（#22, 29クリップ/174.5秒）での CER/WER/RTF、
     `decode ≒ 固定費 + 限界コスト×audio_s` の分離、ロード時間、peak RSS
  2. MT: Hy-MT2 の en/zh 遅延と常駐メモリ
  3. 競合下: ASRとMTを同時に走らせたときの劣化幅
  4. 区切り: 現行VAD only での 字幕カード数 / 平均文字数 / 不自然な分割数 /
     endpoint latency / Hy-MT2 呼び出し回数
  5. E2E: 実サーバー＋擬似生徒10名で first caption latency / final latency /
     audio_queue_seconds / CPU / RSS

数値は開発機のもので、学校実機（Core i5）はこれより遅い前提で読むこと。
音源は合成音声なので、CER は**エンジン間の相対比較にだけ**使える（絶対値の保証はしない）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.bench import (  # noqa: E402
    ASR_LABELS,
    run_phase_subprocess,
    system_info,
)
from server.config import load_config  # noqa: E402

# ベースラインを構成するベンチフェーズ。既定エンジン構成（small + hy-mt2）を軸に、
# ASRエンジン判定（#28）の比較対象として kotoba も測る
BENCH_PHASES = [
    "asr-ext:small",
    "asr-ext:kotoba",
    "mt:hy-mt2",
    "concurrent:small:hy-mt2",
    "segmentation:small",
]
E2E_ENGINE = "hy-mt2"


def run_e2e(minutes: float, work_dir: Path) -> dict | None:
    """実サーバーを起動して2分リプレイを走らせ、その生JSONを返す。

    受け入れ試験本体（docs/accept/）を上書きしないよう、出力は作業ディレクトリへ逃がす。
    config.yaml も書き換えさせない（--no-config-update）。
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"=== e2e: replay {minutes}分 / engine={E2E_ENGINE} ===", flush=True)
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "replay_client.py"),
            "--minutes", str(minutes),
            "--engine", E2E_ENGINE,
            "--out-dir", str(work_dir),
            "--no-config-update",
        ],
        cwd=str(ROOT),
    )
    raw = next(iter(sorted(work_dir.glob("*-acceptance.json"))), None)
    if raw is None:
        print(f"!!! e2e のレポートが見つからない (exit {proc.returncode})")
        return None
    payload = json.loads(raw.read_text(encoding="utf-8"))
    return payload["results"][0] if payload.get("results") else None


# ---- レポート ----


def _fmt(value: object, suffix: str = "") -> str:
    return "-" if value is None else f"{value}{suffix}"


def _pct(rate: float | None) -> str:
    return "-" if rate is None else f"{rate * 100:.2f}%"


def asr_section(add, results: dict) -> None:
    add("## 1. ASR 精度（拡張コーパス ja_ext）")
    add("")
    models = [(k, results[k]) for k in ("asr-ext:small", "asr-ext:kotoba") if results.get(k)]
    if not models:
        add("計測失敗。")
        add("")
        return
    ref = models[0][1]
    add(
        f"音源: `tests/fixtures/ja_ext/index.json`（{ref['clips']}クリップ / "
        f"{ref['audio_s_total']}秒、#22 で拡張）。CER は NFKC正規化＋句読点除去後の文字単位。"
    )
    add("")
    add("| モデル | CER | CER(句読点込み) | WER | 置換/欠落/挿入 | RTF中央値 | RTF最大 | ロード | peak RSS |")
    add("|--------|-----|----------------|-----|---------------|----------|--------|-------|---------|")
    for key, r in models:
        c = r["cer"]
        add(
            f"| {ASR_LABELS[r['model']]} | **{_pct(c['rate'])}** | {_pct(r['cer_raw_rate'])} | "
            f"{_pct(r['wer']['rate'])} | {c['substitutions']}/{c['deletions']}/{c['insertions']} | "
            f"{r['rtf_median']} | {r['rtf_max']} | {r['load_s']}s | {r['peak_rss_mb']}MB |"
        )
    add("")
    add(
        f"- WER のトークナイザは `{ref['wer_tokenizer']}`（文字種の切れ目で切る近似）。"
        "**別トークナイザの WER と比べないこと**。日本語の主指標は CER"
    )
    add(
        "- 句読点を除いて採点しているのは、#21 の通り ReazonSpeech K2 が語彙に `。！？` を"
        "持たないため。表記仕様の差でエンジン判定（#28）が決まらないようにしている"
    )
    add("")
    add("### カテゴリ別 CER")
    add("")
    cats = sorted({c for _, r in models for c in r["by_category"]})
    add("| モデル | " + " | ".join(cats) + " |")
    add("|--------|" + "|".join(["---"] * len(cats)) + "|")
    for key, r in models:
        cells = [_pct(r["by_category"].get(c, {}).get("cer")) for c in cats]
        add(f"| {ASR_LABELS[r['model']]} | " + " | ".join(cells) + " |")
    add("")
    add("### デコード時間の固定費と限界コスト")
    add("")
    add("発話を細かく割る変更（#27 の turn 連結、#29 の partial 字幕）は固定費を払う回数を")
    add("変える。`decode ≈ 固定費 + 限界コスト×audio_s` に分けておくと影響を見積もれる。")
    add("")
    add("| モデル | 固定費 | 限界コスト | R² | 標本数 |")
    add("|--------|-------|-----------|-----|-------|")
    for key, r in models:
        m = r["cost_model"]
        if m:
            add(
                f"| {ASR_LABELS[r['model']]} | {m['fixed_s']}s | "
                f"{m['marginal_s_per_audio_s']}s / audio_s | {m['r2']} | {m['n']} |"
            )
        else:
            add(f"| {ASR_LABELS[r['model']]} | - | - | - | - |")
    add("")
    add("> 回帰の対象は発話クリップのみ。無発話クリップは下の通り桁が違い、混ぜると歪む。")
    add("")
    add("### 無発話（雑音のみ）での挙動")
    add("")
    add("実運用ではVADが雑音のみの区間を落とすためASRまで届かない（区切りの節: カード0件）。")
    add("この行は**VADを緩める変更を入れたときに何を払うことになるか**の値として読む。")
    add("")
    add("> **この行だけ run 間の振れが大きい**（whisper small で観測: デコード 11.4〜18.4s、")
    add("> 幻覚 0〜14文字）。無発話入力では温度フォールバックが走り、CPU int8 の非決定性が")
    add("> 効くため。**1回の差を回帰と読まないこと**。")
    add("")
    add("| モデル | 音源長 | デコード時間 | RTF | 幻覚文字数 |")
    add("|--------|-------|------------|-----|-----------|")
    for key, r in models:
        for e in r["empty_reference"]:
            add(
                f"| {ASR_LABELS[r['model']]} | {e['audio_s']}s | {e['decode_s']}s | "
                f"{e['rtf']} | {e['hallucinated_chars']} |"
            )
    add("")


def mt_section(add, results: dict) -> None:
    add("## 2. MT（Hy-MT2-1.8B）")
    add("")
    r = results.get("mt:hy-mt2")
    if not r:
        add("計測失敗。")
        add("")
        return
    add("| 言語 | 中央値 | 最大 |")
    add("|------|-------|------|")
    for lang in ("en", "zh"):
        add(f"| {lang} | {r['ms_median'][lang]}ms | {r['ms_max'][lang]}ms |")
    add(f"| en+zh 合計 | {r['pair_ms_median']}ms | - |")
    add("")
    add(f"- ロード {r['load_s']}s / 常駐増分 {r['rss_mb']}MB")
    add("- 翻訳キャッシュとスレッド配分の最適化は #26。この値がその before")
    add("")


def concurrent_section(add, results: dict) -> None:
    add("## 3. 競合下（ASR と MT を同時に走らせる）")
    add("")
    conc = results.get("concurrent:small:hy-mt2")
    asr = results.get("asr-ext:small")
    mt = results.get("mt:hy-mt2")
    if not conc:
        add("計測失敗。")
        add("")
        return
    add("| 指標 | 単独 | 競合下 | 劣化 |")
    add("|------|------|-------|------|")
    if asr:
        solo = asr["decode_s_median"]
        cont = conc["asr_decode_s_median"]
        add(f"| ASR デコード中央値 | {solo}s | {cont}s | ×{cont / solo:.2f} |")
    if mt:
        solo_ms = mt["ms_median"]["en"]
        cont_ms = conc["mt_ms_median"]
        add(f"| MT 中央値 | {solo_ms}ms | {cont_ms}ms | ×{cont_ms / solo_ms:.2f} |")
    add(f"| ピークRSS | - | {conc['peak_rss_mb']}MB | - |")
    add("")
    add("> ASR の単独値は拡張コーパス、競合下は既存10文の音源。**音源が違うので倍率は目安**。")
    add("> 揃えた比較が要るなら競合フェーズも拡張コーパスに移すこと（#28 の判定材料になる場合）。")
    add("")


def segmentation_section(add, results: dict) -> None:
    add("## 4. 区切り（現行 VAD only）")
    add("")
    r = results.get("segmentation:small")
    if not r:
        add("計測失敗。")
        add("")
        return
    add(
        f"採点対象は区切り注釈のあるクリップのみ（{r['clips']}件 / {r['audio_s_total']}秒）。"
        "既存10文は `boundary_annotated: false` なので #22 の結論に従い除外している。"
    )
    add("")
    add("| 指標 | 値 |")
    add("|------|-----|")
    add(f"| 字幕カード数 | {r['cards']}（期待 turn 数 {r['expected_turns']} / 比 {r['cards_per_expected_turn']}） |")
    add(f"| 平均文字数 | {_fmt(r['card_chars_mean'])}（中央値 {_fmt(r['card_chars_median'])}） |")
    add(f"| **不自然な分割数** | **{r['unnatural_splits']}** |")
    add(f"| 本当の切れ目の検出 | {r['natural_breaks_hit']} / {r['natural_breaks_total']} |")
    add(f"| endpoint latency 中央値 | {_fmt(r['endpoint_latency_ms_median'], 'ms')} |")
    add(f"| endpoint latency 最大 | {_fmt(r['endpoint_latency_ms_max'], 'ms')} |")
    add(f"| Hy-MT2 呼び出し回数 | {r['mt_calls']}（{r['cards']}発話 × {r['langs']}言語） |")
    add("")
    add(
        f"- VAD は `{r['vad']}` / `min_silence_ms={r['min_silence_ms']}` / "
        f"フレーム {r['frame_ms']}ms。endpoint latency は無音フレームを数え切るまでの時間で、"
        "設定から決まる定数になる（可変にするのが #27 の狙いのひとつ）"
    )
    add("- 不自然な分割 = `gaps[].natural_break == false` のギャップ上で切れた件数（#22 の注釈で機械判定）")
    add("- 本当の切れ目 = `natural_break == true` のギャップ。ここで切れているぶんには問題ない")
    add("")
    worst = sorted(r["per_clip"], key=lambda c: -c["unnatural_splits"])[:5]
    if worst and worst[0]["unnatural_splits"]:
        add("### 不自然に割られたクリップ")
        add("")
        add("| クリップ | 種別 | カード数 | 期待turn | 不自然な分割 |")
        add("|---------|------|---------|---------|------------|")
        for c in worst:
            if c["unnatural_splits"]:
                add(
                    f"| `{c['id']}` | {c['category']} | {c['cards']} | "
                    f"{_fmt(c['expected_turns'])} | {c['unnatural_splits']} |"
                )
        add("")


def e2e_section(add, e2e: dict | None, minutes: float) -> None:
    add("## 5. E2E（実サーバー + 擬似生徒）")
    add("")
    if not e2e:
        add("未計測（`--skip-e2e`、または計測失敗）。")
        add("")
        return
    lat = e2e.get("latency")
    mem = e2e.get("memory")
    cpu = e2e.get("cpu_percent")
    aq = e2e.get("audio_queue_seconds")
    qd = e2e.get("queue_depth")
    add(
        f"{e2e['minutes']}分 / 擬似生徒 {e2e['students']}名（en・zh 半々）/ "
        f"エンジン {e2e['engine']} / 音源は fixture ループ合成。"
    )
    add("")
    add("| 指標 | 値 |")
    add("|------|-----|")
    add(f"| caption 数 | {e2e['captions']} |")
    add(f"| **first caption latency** | {_fmt(e2e.get('first_caption_s'), 's')}（配信開始→最初の字幕。最初の発話が終わるまでの待ちを含む） |")
    if lat:
        add(f"| **final latency 中央値** | {lat['median_s']}s（基準 ≤5s） |")
        add(f"| final latency p95 | {lat['p95_s']}s |")
        add(f"| final latency 最大 | {lat['max_s']}s（基準 ≤8s） |")
    if aq:
        add(f"| `audio_queue_seconds` 中央値 | {aq['median']}s |")
        add(f"| `audio_queue_seconds` p95 / 最大 | {aq['p95']}s / {aq['max']}s |")
    if qd:
        add(f"| キュー深度 中央値 / 最大 | {qd['median']} / {qd['max']} |")
    add(f"| 過負荷サンプル | {_fmt(e2e.get('overloaded_samples'))} |")
    if cpu:
        add(f"| CPU 中央値 / p95 / 最大 | {cpu['median']}% / {cpu['p95']}% / {cpu['max']}%（1コア=100%、論理{cpu['cores']}コア） |")
    if mem:
        add(f"| RSS ピーク | {mem['peak_mb']}MB（基準 ≤5000MB） |")
        add(f"| RSS 増分（前半→後半） | {mem['increase_mb']:+}MB |")
    add(f"| 切断 / 復元失敗 / クラッシュ | {e2e['disconnects']} / {e2e['reconnect_failures']} / {e2e['crashes']} |")
    add("")
    add(
        "- `audio_queue_seconds` は ASR待ち＋処理中の音声の秒数。2秒間隔のサンプリングなので、"
        "中央値0 / 最大が1発話ぶんという分布は「常に空か、1発話が処理中か」を意味する。"
        "**滞留が積み上がっているという意味ではない**（積み上がりは #25 の負荷条件で初めて出る）"
    )
    add("- CPU はサーバープロセスツリー合計。論理コア数×100% が上限")
    add("")
    add(
        f"> **{minutes}分は45分の受け入れ試験の代わりにならない**（N-08 のドリフト判定には短すぎる）。"
        "メモリ増分とキュー滞留の「初期値」として読むこと。45分の実走は別途必要。"
    )
    add("")


def build_report(info: dict, results: dict, e2e: dict | None, minutes: float) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# ベースライン計測（イシュー#24） — {info['date']}")
    add("")
    add(
        f"- 計測機: **{info['cpu']}**（{info['cores']}C/{info['threads']}T, "
        f"RAM {info['ram_gb']}GB, {info['os']}, Python {info['python']}）"
    )
    add("- 再現: `python scripts/baseline.py`")
    add("")
    add("> **この数値の読み方**")
    add("> - 開発機での実測。**学校実機（Core i5）はこれより遅い前提**で読む（実機再計測は未実施）")
    add("> - 音源は SAPI 合成音声。CER の絶対値は実教室マイクの精度ではない。")
    add(">   **エンジン間・変更前後の相対比較にだけ使える**")
    add("> - 改善判定はこのファイルの数値と比べて行う（#25〜#30）")
    add("")
    asr_section(add, results)
    mt_section(add, results)
    concurrent_section(add, results)
    segmentation_section(add, results)
    e2e_section(add, e2e, minutes)
    handoff_section(add, results, e2e)
    return "\n".join(lines) + "\n"


def handoff_section(add, results: dict, e2e: dict | None) -> None:
    add("## この基準線をどう使うか")
    add("")
    seg = results.get("segmentation:small")
    small = results.get("asr-ext:small")
    add("| チケット | 見るべき before 値 |")
    add("|---------|------------------|")
    if e2e:
        aq = e2e.get("audio_queue_seconds")
        add(
            f"| #25 P0是正 | `audio_queue_seconds` 中央値 {_fmt(aq['median'] if aq else None, 's')} / "
            f"最大 {_fmt(aq['max'] if aq else None, 's')}、キュー深度、RSS 増分 |"
        )
    if results.get("mt:hy-mt2"):
        mt = results["mt:hy-mt2"]
        add(f"| #26 Hy-MT2 最適化 | en {mt['ms_median']['en']}ms / zh {mt['ms_median']['zh']}ms、常駐 {mt['rss_mb']}MB、競合下の劣化 |")
    if seg:
        add(
            f"| #27 区切り | 不自然な分割 {seg['unnatural_splits']}件 / カード {seg['cards']} / "
            f"平均 {_fmt(seg['card_chars_mean'])}字 / MT呼び出し {seg['mt_calls']}回 |"
        )
    if small:
        add(f"| #28 ASRエンジン判定 | CER {_pct(small['cer']['rate'])}、RTF中央値 {small['rtf_median']}、固定費/限界コスト |")
    if e2e:
        add(f"| #29 partial字幕 | first caption latency {_fmt(e2e.get('first_caption_s'), 's')} |")
    add("| #30 先生UI（遅延内訳） | asr_ms / mt_ms / delay_ms の内訳（このレポートは合計値のみ） |")
    add("")
    add("**まだ測れていないもの**（意図的に範囲外）:")
    add("")
    add("- 学校実機（Core i5）での再計測。すべての数値が開発機の楽観側")
    add("- 実教室マイクでの CER（合成音声では代替できない。実地検証 #19 の領域）")
    add("- 45〜60分の連続動作でのドリフト（本レポートの E2E は短時間のスモーク）")
    add("- 遅延の内訳（ASR / MT / 配信のどこで何秒使ったか）。#30 の計装が入ってから")
    add("")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=2, help="E2E の試験長（分・既定2）")
    parser.add_argument("--skip-e2e", action="store_true", help="E2E を省く（ベンチのみ）")
    parser.add_argument("--models-dir", default=None)
    parser.add_argument("--out-dir", default=str(ROOT / "docs" / "bench"))
    parser.add_argument("--report-from", default=None, help="既存の生JSONからレポートのみ再生成")
    parser.add_argument(
        "--keep-work", action="store_true", help="E2E の作業ディレクトリを消さない"
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    if args.report_from:
        raw = json.loads(Path(args.report_from).read_text(encoding="utf-8"))
        md = build_report(
            raw["system"],
            {k: v for k, v in raw["results"].items() if v},
            raw.get("e2e"),
            raw.get("e2e_minutes", args.minutes),
        )
        path = Path(args.report_from).with_suffix(".md")
        path.write_text(md, encoding="utf-8")
        print(f"レポート再生成: {path}")
        return 0

    config = load_config(ROOT / "config.yaml")
    models_dir = Path(args.models_dir) if args.models_dir else config.models.resolved_dir
    info = system_info()
    print(f"machine: {info['cpu']} / {info['cores']}C{info['threads']}T / {info['ram_gb']}GB")

    results: dict[str, dict | None] = {}
    for phase in BENCH_PHASES:
        results[phase] = run_phase_subprocess(phase, models_dir)

    work_dir = out_dir / "_work"
    e2e = None if args.skip_e2e else run_e2e(args.minutes, work_dir)
    if not args.keep_work and work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{info['date']}-baseline"
    (out_dir / f"{stem}.json").write_text(
        json.dumps(
            {"system": info, "results": results, "e2e": e2e, "e2e_minutes": args.minutes},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    md = build_report(info, {k: v for k, v in results.items() if v}, e2e, args.minutes)
    (out_dir / f"{stem}.md").write_text(md, encoding="utf-8")
    print(f"\nレポート: {out_dir / (stem + '.md')}")
    print(f"生データ: {out_dir / (stem + '.json')}")
    missing = [k for k, v in results.items() if not v]
    if missing:
        print(f"!!! 失敗したフェーズ: {', '.join(missing)}")
    return 0 if not missing and (args.skip_e2e or e2e) else 1


if __name__ == "__main__":
    sys.exit(main())
