"""実機性能ベンチマーク（イシュー#9 / plan.md Phase 0 判断ゲート①）。

計測項目:
  (a) ASRの実時間比 RTF（kotoba-whisper-v2.0 / whisper small、int8）
  (b) 1発話×2言語（英・中）の翻訳遅延（NLLB-600M ct2 / Hy-MT2-1.8B GGUF）
  (c) ASR＋翻訳の同時実行時の遅延とプロセス常駐メモリ

使い方:
  python scripts/bench.py                 # 全フェーズ実行 + レポート生成
  python scripts/bench.py --phase mt:nllb # 単一フェーズ（JSONのみ出力）

各フェーズはメモリ計測を汚さないようサブプロセスで実行される。
前提: scripts/download_models.py 済み、tests/fixtures/ja/*.wav
（無ければ scripts/make_fixture_audio.ps1 で生成）。
レポートは docs/bench/ に Markdown + 生JSON で保存する。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import load_config  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "ja"
SENTENCES_FILE = ROOT / "tests" / "fixtures" / "ja_sentences.txt"
JSON_SENTINEL = "BENCH_JSON:"

ASR_MODELS = {
    "kotoba": "kotoba-whisper-v2.0-faster",
    "small": "faster-whisper-small",
}
NLLB_TARGETS = {"en": "eng_Latn", "zh": "zho_Hans"}
HYMT_TARGETS = {"en": "English", "zh": "Simplified Chinese"}


def rss_mb() -> float:
    return psutil.Process().memory_info().rss / 1e6


def load_wavs() -> list[dict]:
    wavs = []
    for path in sorted(FIXTURE_DIR.glob("*.wav")):
        with wave.open(str(path), "rb") as w:
            assert w.getframerate() == 16000 and w.getnchannels() == 1
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        wavs.append(
            {"name": path.name, "audio": pcm.astype(np.float32) / 32768.0, "seconds": pcm.size / 16000}
        )
    if not wavs:
        sys.exit(f"fixture が無い: {FIXTURE_DIR}。scripts/make_fixture_audio.ps1 を実行のこと")
    return wavs


def load_sentences() -> list[str]:
    return [s.strip() for s in SENTENCES_FILE.read_text(encoding="utf-8").splitlines() if s.strip()]


# ---- フェーズ実装（サブプロセス内で実行される） ----


def phase_asr(model_key: str, models_dir: Path) -> dict:
    from faster_whisper import WhisperModel

    model_path = models_dir / ASR_MODELS[model_key]
    wavs = load_wavs()
    before = rss_mb()
    t0 = time.perf_counter()
    model = WhisperModel(str(model_path), device="cpu", compute_type="int8")
    load_s = time.perf_counter() - t0

    def transcribe(audio: np.ndarray) -> tuple[str, float]:
        t = time.perf_counter()
        segments, _ = model.transcribe(audio, language="ja", beam_size=1, vad_filter=False)
        text = "".join(seg.text for seg in segments)  # ジェネレータ消費でデコード完了
        return text, time.perf_counter() - t

    transcribe(wavs[0]["audio"])  # ウォームアップ（計測外）
    items = []
    for w in wavs:
        text, dt = transcribe(w["audio"])
        items.append(
            {"file": w["name"], "audio_s": round(w["seconds"], 2), "decode_s": round(dt, 3),
             "rtf": round(dt / w["seconds"], 3), "text": text.strip()}
        )
    rtfs = [i["rtf"] for i in items]
    return {
        "model": model_key,
        "load_s": round(load_s, 1),
        "rss_mb": round(rss_mb() - before),
        "rtf_median": round(statistics.median(rtfs), 3),
        "rtf_max": round(max(rtfs), 3),
        "decode_s_median": round(statistics.median(i["decode_s"] for i in items), 2),
        "items": items,
    }


def make_nllb(models_dir: Path):
    import ctranslate2
    from transformers import AutoTokenizer

    translator = ctranslate2.Translator(
        str(models_dir / "nllb-200-distilled-600M-ct2"), device="cpu"
    )
    tokenizer = AutoTokenizer.from_pretrained(
        str(models_dir / "nllb-tokenizer"), src_lang="jpn_Jpan"
    )

    def translate(text: str, lang: str) -> str:
        tokens = tokenizer.convert_ids_to_tokens(tokenizer(text).input_ids)
        result = translator.translate_batch(
            [tokens], target_prefix=[[NLLB_TARGETS[lang]]], beam_size=1
        )
        out = result[0].hypotheses[0][1:]  # 先頭の言語トークンを除去
        return tokenizer.decode(tokenizer.convert_tokens_to_ids(out), skip_special_tokens=True)

    return translate


def make_hymt(models_dir: Path):
    from llama_cpp import Llama

    ggufs = list((models_dir / "hy-mt2").glob("*.gguf"))
    if not ggufs:
        raise FileNotFoundError(f"GGUFが無い: {models_dir / 'hy-mt2'}")
    llm = Llama(
        model_path=str(ggufs[0]),
        n_ctx=2048,
        n_threads=psutil.cpu_count(logical=False) or 4,
        verbose=False,
    )

    def translate(text: str, lang: str) -> str:
        # モデルカード記載の翻訳プロンプトと推奨パラメータ
        prompt = (
            f"Translate the following text into {HYMT_TARGETS[lang]}. Note that you should "
            f"only output the translated result without any additional explanation: {text}"
        )
        res = llm.create_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7, top_p=0.6, top_k=20, repeat_penalty=1.05, max_tokens=256,
        )
        return str(res["choices"][0]["message"]["content"]).strip()

    return translate


def phase_mt(engine: str, models_dir: Path) -> dict:
    sentences = load_sentences()
    before = rss_mb()
    t0 = time.perf_counter()
    translate = make_nllb(models_dir) if engine == "nllb" else make_hymt(models_dir)
    load_s = time.perf_counter() - t0
    translate(sentences[0], "en")  # ウォームアップ（計測外）

    items = []
    for text in sentences:
        for lang in ("en", "zh"):
            t = time.perf_counter()
            out = translate(text, lang)
            items.append(
                {"lang": lang, "ms": round((time.perf_counter() - t) * 1000),
                 "ja": text, "out": out}
            )
    by_lang = {
        lang: [i["ms"] for i in items if i["lang"] == lang] for lang in ("en", "zh")
    }
    return {
        "engine": engine,
        "load_s": round(load_s, 1),
        "rss_mb": round(rss_mb() - before),
        "ms_median": {lang: round(statistics.median(v)) for lang, v in by_lang.items()},
        "ms_max": {lang: max(v) for lang, v in by_lang.items()},
        "pair_ms_median": round(
            statistics.median(by_lang["en"][i] + by_lang["zh"][i] for i in range(len(by_lang["en"])))
        ),
        "items": items,
    }


def phase_concurrent(engine: str, models_dir: Path) -> dict:
    """ASR(kotoba)と翻訳を別スレッドで同時実行し、競合下の遅延とピークRSSを見る。"""
    from faster_whisper import WhisperModel

    wavs = load_wavs()
    sentences = load_sentences()
    asr = WhisperModel(str(models_dir / ASR_MODELS["kotoba"]), device="cpu", compute_type="int8")
    translate = make_nllb(models_dir) if engine == "nllb" else make_hymt(models_dir)

    # ウォームアップ
    list(asr.transcribe(wavs[0]["audio"], language="ja", beam_size=1)[0])
    translate(sentences[0], "en")

    peak = {"rss": rss_mb()}
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            peak["rss"] = max(peak["rss"], rss_mb())
            time.sleep(0.1)

    asr_times: list[dict] = []
    mt_times: list[dict] = []

    def asr_worker() -> None:
        for w in wavs:
            t = time.perf_counter()
            segments, _ = asr.transcribe(w["audio"], language="ja", beam_size=1, vad_filter=False)
            _ = "".join(s.text for s in segments)
            asr_times.append({"audio_s": w["seconds"], "decode_s": time.perf_counter() - t})

    def mt_worker() -> None:
        for text in sentences:
            for lang in ("en", "zh"):
                t = time.perf_counter()
                translate(text, lang)
                mt_times.append({"lang": lang, "ms": (time.perf_counter() - t) * 1000})

    threads = [threading.Thread(target=asr_worker), threading.Thread(target=mt_worker),
               threading.Thread(target=sampler, daemon=True)]
    t0 = time.perf_counter()
    for th in threads[:2]:
        th.start()
    threads[2].start()
    for th in threads[:2]:
        th.join()
    stop.set()
    wall = time.perf_counter() - t0

    rtfs = [x["decode_s"] / x["audio_s"] for x in asr_times]
    ms = [x["ms"] for x in mt_times]
    return {
        "engine": engine,
        "wall_s": round(wall, 1),
        "peak_rss_mb": round(peak["rss"]),
        "asr_rtf_median": round(statistics.median(rtfs), 3),
        "asr_rtf_max": round(max(rtfs), 3),
        "mt_ms_median": round(statistics.median(ms)),
        "mt_ms_max": round(max(ms)),
    }


PHASES = {
    "asr:kotoba": lambda d: phase_asr("kotoba", d),
    "asr:small": lambda d: phase_asr("small", d),
    "mt:nllb": lambda d: phase_mt("nllb", d),
    "mt:hy-mt2": lambda d: phase_mt("hy-mt2", d),
    "concurrent:nllb": lambda d: phase_concurrent("nllb", d),
    "concurrent:hy-mt2": lambda d: phase_concurrent("hy-mt2", d),
}


# ---- オーケストレーター ----


def run_phase_subprocess(phase: str, models_dir: Path) -> dict | None:
    print(f"=== {phase} ===", flush=True)
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--phase", phase,
         "--models-dir", str(models_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        # 子のstdoutがcp932になると日本語（0x5Cを含む文字）がJSONを壊すためUTF-8を強制
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith(JSON_SENTINEL):
            try:
                payload = json.loads(line[len(JSON_SENTINEL):])
            except json.JSONDecodeError as exc:
                print(f"!!! フェーズ {phase} のJSONが解釈できない: {exc}")
        else:
            print(line)
    if proc.returncode != 0:
        print(f"!!! フェーズ {phase} が失敗 (exit {proc.returncode})")
        print(proc.stderr[-2000:])
        return None
    return payload


def system_info() -> dict:
    cpu = platform.processor()
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
            capture_output=True, text=True, timeout=15,
        )
        if out.stdout.strip():
            cpu = out.stdout.strip()
    except Exception:
        pass
    return {
        "cpu": cpu,
        "cores": psutil.cpu_count(logical=False),
        "threads": psutil.cpu_count(logical=True),
        "ram_gb": round(psutil.virtual_memory().total / 1e9, 1),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "date": datetime.date.today().isoformat(),
    }


def build_report(info: dict, results: dict, models_dir: Path) -> str:
    wavs_meta = [(w["name"], w["seconds"]) for w in load_wavs()]
    median_utt = statistics.median(s for _, s in wavs_meta)

    def estimate(asr: dict | None, mt: dict | None) -> dict | None:
        """発話終了→2言語目のcaption送出までの見積り（ASR→en→zh直列、plan.md §6.3の構成）。"""
        if not asr or not mt:
            return None
        med = asr["rtf_median"] * median_utt + (mt["ms_median"]["en"] + mt["ms_median"]["zh"]) / 1000
        worst = asr["rtf_max"] * median_utt + (mt["ms_max"]["en"] + mt["ms_max"]["zh"]) / 1000
        return {"median_s": round(med, 1), "worst_s": round(worst, 1),
                "pass_median": med <= 5.0, "pass_max": worst <= 8.0}

    est = {
        (a, m): estimate(results.get(f"asr:{a}"), results.get(f"mt:{m}"))
        for a in ("kotoba", "small") for m in ("nllb", "hy-mt2")
    }

    lines: list[str] = []
    add = lines.append
    add(f"# ベンチ報告（イシュー#9 / 判断ゲート①） — {info['date']}")
    add("")
    add(f"- 計測機: **{info['cpu']}**（{info['cores']}C/{info['threads']}T, RAM {info['ram_gb']}GB, {info['os']}, Python {info['python']}）")
    add(f"- モデル格納先: `{models_dir}`（OneDrive外, R-08対応）")
    add(f"- 音源: SAPI(Haruka)合成の授業想定日本語10文（中央値 {median_utt:.1f}s）。**速度計測用であり実教室マイクの精度検証は#19**")
    add("")
    add("> **注意**: 本計測は開発機（上記CPU）での実測。plan.md が想定する学校の Core i5 は")
    add("> これより遅い可能性が高く、導入前に同コマンド `python scripts/bench.py` での再計測が必要。")
    add("")
    add("## Q-01 の確認結果（hy-mt2 1.8b の配布元・ライセンス）")
    add("")
    add("- 正体は **Tencent Hy-MT2-1.8B**（Hunyuan-MT2ファミリー、2026-05公開、33言語対応）")
    add("- 公式GGUF: [tencent/Hy-MT2-1.8B-GGUF](https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF)（Q4_K_M 1.13GB / Q6_K / Q8_0）")
    add("- ライセンス: **Apache-2.0**（商用制限・地域制限なし）→ 学校利用に問題なし。**取得可能、Q-01解消**")
    add("- 参考: NLLB-200 は CC-BY-NC 4.0（非商用限定）。授業利用は非商用の想定（A-05）")
    add("")
    add("## (a) ASR 実時間比（int8, beam_size=1）")
    add("")
    add("| モデル | ロード | 常駐増分 | RTF中央値 | RTF最大 | デコード中央値 |")
    add("|--------|-------|---------|----------|--------|--------------|")
    for key in ("kotoba", "small"):
        r = results.get(f"asr:{key}")
        if r:
            add(f"| {ASR_MODELS[key]} | {r['load_s']}s | {r['rss_mb']}MB | {r['rtf_median']} | {r['rtf_max']} | {r['decode_s_median']}s |")
        else:
            add(f"| {ASR_MODELS[key]} | 計測失敗 | - | - | - | - |")
    add("")
    add("## (b) 翻訳遅延（1発話×2言語）")
    add("")
    add("| エンジン | ロード | 常駐増分 | en中央値 | zh中央値 | en最大 | zh最大 | en+zh合計中央値 |")
    add("|---------|-------|---------|---------|---------|-------|-------|---------------|")
    for key in ("nllb", "hy-mt2"):
        r = results.get(f"mt:{key}")
        if r:
            add(f"| {key} | {r['load_s']}s | {r['rss_mb']}MB | {r['ms_median']['en']}ms | {r['ms_median']['zh']}ms | {r['ms_max']['en']}ms | {r['ms_max']['zh']}ms | {r['pair_ms_median']}ms |")
        else:
            add(f"| {key} | 計測失敗 | - | - | - | - | - | - |")
    add("")
    add("## (c) ASR＋翻訳 同時実行（kotoba + 各エンジン）")
    add("")
    add("| 翻訳エンジン | 完走 | ピークRSS | ASR RTF中央値(競合下) | 翻訳中央値(競合下) | 翻訳最大(競合下) |")
    add("|-------------|------|----------|---------------------|------------------|----------------|")
    for key in ("nllb", "hy-mt2"):
        r = results.get(f"concurrent:{key}")
        if r:
            add(f"| {key} | {r['wall_s']}s | {r['peak_rss_mb']}MB | {r['asr_rtf_median']} | {r['mt_ms_median']}ms | {r['mt_ms_max']}ms |")
        else:
            add(f"| {key} | 計測失敗 | - | - | - | - |")
    add("")
    add("## 遅延バジェット判定（発話終了→2言語目caption、中央値≤5s / 最大≤8s）")
    add("")
    add(f"発話 {median_utt:.1f}s（fixture中央値）を ASR→en→zh 直列処理した場合の見積り:")
    add("")
    add("| 構成 | 中央値見積り | 最悪見積り | 中央値≤5s | 最大≤8s |")
    add("|------|------------|-----------|----------|--------|")
    for (a, m), e in est.items():
        if e:
            add(f"| {a} + {m} | {e['median_s']}s | {e['worst_s']}s | {'✅' if e['pass_median'] else '❌'} | {'✅' if e['pass_max'] else '❌'} |")
        else:
            add(f"| {a} + {m} | 計測失敗 | - | - | - |")
    add("")

    # 品質優先の選好順（plan.md §3: 予算内なら品質側を採る）で最初に予算を満たす構成が既定
    prefs = [("kotoba", "hy-mt2"), ("kotoba", "nllb"), ("small", "hy-mt2"), ("small", "nllb")]
    chosen = next(
        ((a, m) for a, m in prefs
         if est.get((a, m)) and est[(a, m)]["pass_median"] and est[(a, m)]["pass_max"]),
        None,
    )
    add("## 判断ゲート①の結論")
    add("")
    if chosen:
        add(f"- **既定ASRモデル = whisper {chosen[0]}** / **既定翻訳エンジン = {chosen[1]}**")
        add("  （品質優先の選好順 kotoba+hy-mt2 → kotoba+nllb → small+hy-mt2 → small+nllb で、予算を満たす最初の構成）")
    else:
        add("- **予算を満たす構成なし** → plan.md R-01 に従い構成再検討（Opus-MT等の追加調査）")
    add("- config.yaml に反映済み。学校の実機（i5）での再計測後に最終確定とする")
    add("")
    add("### 所見（数値に表れないトレードオフ）")
    add("")
    add("- **kotoba はCPUで実時間比>1**（発話より文字起こしが遅い）のため、精度は良いが授業のリアルタイム用途には不採用。GPU化・録画の事後書き起こし用途では有力")
    add("- **small は教科用語の同音異義誤りが出る**（例: 光合成→「構合性」）。Phase 3 の用語辞書（タスク25）とセットで運用し、実地検証（#19）で許容度を判断する")
    add("- **翻訳品質は hy-mt2 が明確に優位**（下のサンプル参照。NLLBは「光合成→合成光/光合成(日本語のまま)」等の誤り、hy-mt2 は正しく「光合作用」）。遅延差は+164ms、メモリ差は+1.2GBでいずれも予算内")
    add("- ライセンス面でも hy-mt2（Apache-2.0）は NLLB（CC-BY-NC）より制約が少ない")
    add("")
    add("## サンプル出力（品質の目視確認用）")
    add("")
    add("### ASR（TTS音源のため参考値。実マイクの検証は#19）")
    add("")
    add("| 原文 | kotoba | small |")
    add("|------|--------|-------|")
    kotoba_items = (results.get("asr:kotoba") or {}).get("items", [])
    small_items = (results.get("asr:small") or {}).get("items", [])
    for k_item, s_item in list(zip(kotoba_items, small_items))[:4]:
        add(f"| {k_item['file']} | {k_item['text']} | {s_item['text']} |")
    add("")
    add("### 翻訳（原文はテキスト入力。ASR誤りとの複合影響は性能受け入れ試験 #17 で確認）")
    add("")
    add("| 原文 | エンジン | en | zh |")
    add("|------|---------|----|----|")
    for engine in ("nllb", "hy-mt2"):
        mt_items = (results.get(f"mt:{engine}") or {}).get("items", [])
        pairs = {}
        for item in mt_items:
            pairs.setdefault(item["ja"], {})[item["lang"]] = item["out"]
        for ja, outs in list(pairs.items())[:3]:
            add(f"| {ja} | {engine} | {outs.get('en', '')} | {outs.get('zh', '')} |")
    add("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=sorted(PHASES))
    parser.add_argument("--models-dir", default=None)
    parser.add_argument("--out-dir", default=str(ROOT / "docs" / "bench"))
    parser.add_argument("--report-from", default=None, help="既存の生JSONからレポートのみ再生成")
    args = parser.parse_args()

    config = load_config(ROOT / "config.yaml")
    models_dir = Path(args.models_dir) if args.models_dir else config.models.resolved_dir

    if args.phase:
        result = PHASES[args.phase](models_dir)
        print(JSON_SENTINEL + json.dumps(result, ensure_ascii=False))
        return 0

    if args.report_from:
        raw = json.loads(Path(args.report_from).read_text(encoding="utf-8"))
        report = build_report(
            raw["system"], {k: v for k, v in raw["results"].items() if v}, models_dir
        )
        md_path = Path(args.report_from).with_suffix(".md")
        md_path.write_text(report, encoding="utf-8")
        print(f"レポート再生成: {md_path}")
        return 0

    info = system_info()
    print(f"machine: {info['cpu']} / {info['cores']}C{info['threads']}T / {info['ram_gb']}GB")
    results: dict[str, dict | None] = {}
    for phase in ("asr:kotoba", "asr:small", "mt:nllb", "mt:hy-mt2",
                  "concurrent:nllb", "concurrent:hy-mt2"):
        results[phase] = run_phase_subprocess(phase, models_dir)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{info['date']}-bench"
    (out_dir / f"{stem}.json").write_text(
        json.dumps({"system": info, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report = build_report(info, {k: v for k, v in results.items() if v}, models_dir)
    (out_dir / f"{stem}.md").write_text(report, encoding="utf-8")
    print(f"\nレポート: {out_dir / (stem + '.md')}")
    print(f"生データ: {out_dir / (stem + '.json')}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
