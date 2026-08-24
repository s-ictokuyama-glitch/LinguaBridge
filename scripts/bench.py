"""実機性能ベンチマーク（イシュー#9 / plan.md Phase 0 判断ゲート①）。

計測項目:
  (a) ASRの実時間比 RTF（kotoba-whisper-v2.0 / whisper small、int8）
  (b) 1発話×2言語（英・中）の翻訳遅延（NLLB-600M ct2 / Hy-MT2-1.8B GGUF）
  (c) ASR＋翻訳の同時実行時の遅延とプロセス常駐メモリ
  (d) 拡張コーパス（#22）での CER/WER と decode の固定費/限界コスト分離（#24）
  (e) 現行VADの区切り品質（字幕カード数・不自然な分割数・endpoint latency）（#24）

使い方:
  python scripts/bench.py                    # 判断ゲート①の8フェーズ + レポート生成
  python scripts/bench.py --phase mt:nllb    # 単一フェーズ（JSONのみ出力）
  python scripts/bench.py --list-phases      # 実行可能なフェーズ一覧

判断ゲート①（2026-07-07）との比較可能性を守るため、既定の実行対象は当時の8フェーズに
固定してある。#24 で足した asr-ext:* / segmentation:* は --phase で明示的に呼ぶ
（束ねて1本のレポートにするのは scripts/baseline.py）。

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
import re
import statistics
import subprocess
import sys
import threading
import time
import wave
from collections import Counter
from pathlib import Path

import numpy as np
import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.text_metrics import (  # noqa: E402
    EditCounts,
    cer_counts,
    corpus_rate,
    normalize_ja,
    resolve_tokenizer,
    wer_counts,
)
from server.config import load_config  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "ja"
SENTENCES_FILE = ROOT / "tests" / "fixtures" / "ja_sentences.txt"
EXT_INDEX = ROOT / "tests" / "fixtures" / "ja_ext" / "index.json"
JSON_SENTINEL = "BENCH_JSON:"


ASR_MODELS = {
    "kotoba": "kotoba-whisper-v2.0-faster",
    "small": "faster-whisper-small",
    # #28 の候補。faster-whisper ではなく sherpa-onnx で動く（下の make_transcriber 参照）
    "reazon": "reazonspeech-k2-v2",
}
ASR_LABELS = {
    "kotoba": "kotoba-whisper-v2.0",
    "small": "whisper small",
    "reazon": "ReazonSpeech K2 v2",
}
# sherpa-onnx で動かすモデルキー。faster-whisper の WhisperModel を作ってはいけない
SHERPA_MODELS = {"reazon"}
NLLB_TARGETS = {"en": "eng_Latn", "zh": "zho_Hans"}
HYMT_TARGETS = {"en": "English", "zh": "Simplified Chinese"}

# 遅延バジェット（PRD N-01: 発話終了→生徒表示）
BUDGET_MEDIAN_S = 5.0
BUDGET_MAX_S = 8.0


def rss_mb() -> float:
    return psutil.Process().memory_info().rss / 1e6


def load_wavs() -> list[dict]:
    wavs = []
    for path in sorted(FIXTURE_DIR.glob("*.wav")):
        with wave.open(str(path), "rb") as w:
            if w.getframerate() != 16000 or w.getnchannels() != 1:
                sys.exit(
                    f"{path.name}: 16kHz mono ではない（{w.getframerate()}Hz/{w.getnchannels()}ch）。"
                    "scripts/make_fixture_audio.ps1 で再生成のこと"
                )
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


def timed_transcribe(model, audio: np.ndarray) -> tuple[str, float]:
    t = time.perf_counter()
    segments, _ = model.transcribe(audio, language="ja", beam_size=1, vad_filter=False)
    text = "".join(seg.text for seg in segments)  # ジェネレータ消費でデコード完了
    return text, time.perf_counter() - t


def make_transcriber(model_key: str, models_dir: Path, **sherpa_kwargs):
    """モデルキーから `(transcribe(audio)->(text, seconds), load_s)` を作る。

    whisper 系と sherpa-onnx でロード手順も推論APIも違うので、ここで吸収して
    フェーズ側をエンジン非依存にする。`audio` は float32 [-1,1] の 16kHz mono。
    """
    before = time.perf_counter()
    if model_key in SHERPA_MODELS:
        from server.asr.sherpa_engine import SherpaOnnxEngine

        engine = SherpaOnnxEngine(models_dir / ASR_MODELS[model_key], **sherpa_kwargs)
        engine.warmup()  # ロード時間にウォームアップ推論1回ぶんが含まれる点は whisper 側と同じ
        load_s = time.perf_counter() - before

        def transcribe(audio: np.ndarray) -> tuple[str, float]:
            pcm16 = (audio * 32768.0).astype(np.int16)
            t = time.perf_counter()
            result = engine.transcribe(pcm16, 16000)
            return result.text, time.perf_counter() - t

        return transcribe, load_s

    from faster_whisper import WhisperModel

    model = WhisperModel(
        str(models_dir / ASR_MODELS[model_key]), device="cpu", compute_type="int8"
    )
    load_s = time.perf_counter() - before

    def transcribe(audio: np.ndarray) -> tuple[str, float]:
        return timed_transcribe(model, audio)

    return transcribe, load_s


def phase_asr(model_key: str, models_dir: Path) -> dict:
    from faster_whisper import WhisperModel

    model_path = models_dir / ASR_MODELS[model_key]
    wavs = load_wavs()
    before = rss_mb()
    t0 = time.perf_counter()
    model = WhisperModel(str(model_path), device="cpu", compute_type="int8")
    load_s = time.perf_counter() - t0

    timed_transcribe(model, wavs[0]["audio"])  # ウォームアップ（計測外）
    items = []
    for w in wavs:
        text, dt = timed_transcribe(model, w["audio"])
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


def make_nllb(models_dir: Path, config):
    import ctranslate2
    from transformers import AutoTokenizer

    translator = ctranslate2.Translator(
        str(models_dir / config.mt.nllb.model_dir), device="cpu"
    )
    tokenizer = AutoTokenizer.from_pretrained(
        str(models_dir / config.mt.nllb.tokenizer_dir), src_lang="jpn_Jpan"
    )

    def translate(text: str, lang: str) -> str:
        tokens = tokenizer.convert_ids_to_tokens(tokenizer(text).input_ids)
        result = translator.translate_batch(
            [tokens], target_prefix=[[NLLB_TARGETS[lang]]], beam_size=1
        )
        out = result[0].hypotheses[0][1:]  # 先頭の言語トークンを除去
        return tokenizer.decode(tokenizer.convert_tokens_to_ids(out), skip_special_tokens=True)

    return translate


def load_hymt(models_dir: Path, config, n_threads: int | None = None):
    """Hy-MT2 の Llama インスタンスを作る。スレッド数は #26 の計測で振るので引数にする。"""
    from llama_cpp import Llama

    gguf = models_dir / config.mt.hy_mt2.gguf_path
    if not gguf.exists():
        raise FileNotFoundError(f"GGUFが無い: {gguf}（scripts/download_models.py を実行のこと）")
    return Llama(
        model_path=str(gguf),
        n_ctx=2048,
        n_threads=n_threads or psutil.cpu_count(logical=False) or 4,
        verbose=False,
    )


def hymt_chat(llm, text: str, target_label: str, *, temperature: float, max_tokens: int = 256) -> str:
    """モデルカード記載の翻訳プロンプトで1回推論する。temperature=0 で貪欲になる。"""
    prompt = (
        f"Translate the following text into {target_label}. Note that you should "
        f"only output the translated result without any additional explanation: {text}"
    )
    res = llm.create_chat_completion(
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature, top_p=0.6, top_k=20, repeat_penalty=1.05, max_tokens=max_tokens,
    )
    return str(res["choices"][0]["message"]["content"]).strip()


def make_hymt(models_dir: Path, config, n_threads: int | None = None):
    llm = load_hymt(models_dir, config, n_threads)

    def translate(text: str, lang: str) -> str:
        # モデルカード記載の推奨サンプリング値。判断ゲート①と同条件を保つ
        return hymt_chat(llm, text, HYMT_TARGETS[lang], temperature=0.7)

    return translate


def phase_mt(engine: str, models_dir: Path, config) -> dict:
    sentences = load_sentences()
    before = rss_mb()
    t0 = time.perf_counter()
    translate = (
        make_nllb(models_dir, config) if engine == "nllb" else make_hymt(models_dir, config)
    )
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


def phase_concurrent(asr_key: str, engine: str, models_dir: Path, config) -> dict:
    """ASRと翻訳を別スレッドで同時実行し、競合下の遅延とピークRSSを見る。"""
    from faster_whisper import WhisperModel

    wavs = load_wavs()
    sentences = load_sentences()
    asr = WhisperModel(str(models_dir / ASR_MODELS[asr_key]), device="cpu", compute_type="int8")
    translate = (
        make_nllb(models_dir, config) if engine == "nllb" else make_hymt(models_dir, config)
    )

    # ウォームアップ
    timed_transcribe(asr, wavs[0]["audio"])
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
            _, dt = timed_transcribe(asr, w["audio"])
            asr_times.append({"audio_s": w["seconds"], "decode_s": dt})

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
        "asr": asr_key,
        "engine": engine,
        "wall_s": round(wall, 1),
        "peak_rss_mb": round(peak["rss"]),
        "asr_decode_s_median": round(statistics.median(x["decode_s"] for x in asr_times), 2),
        "asr_rtf_median": round(statistics.median(rtfs), 3),
        "asr_rtf_max": round(max(rtfs), 3),
        "mt_ms_median": round(statistics.median(ms)),
        "mt_ms_max": round(max(ms)),
    }


# ---- 拡張コーパスでの精度計測（#24） ----


def load_ext_clips() -> list[dict]:
    """#22 の拡張コーパス（tests/fixtures/ja_ext/index.json）を読む。

    index.json が唯一の入口。`text` が CER の正解、`gaps[].natural_break` が
    区切りの正解、`boundary_annotated` が区切りの採点対象かどうかを持つ。
    """
    if not EXT_INDEX.exists():
        sys.exit(
            f"拡張コーパスが無い: {EXT_INDEX}。"
            "scripts/make_fixture_audio.ps1 を実行のこと"
        )
    index = json.loads(EXT_INDEX.read_text(encoding="utf-8"))
    clips = []
    for clip in index["clips"]:
        path = (ROOT / clip["path"]).resolve()
        if not path.exists():
            sys.exit(f"音源が無い: {path}。scripts/make_fixture_audio.ps1 で再生成のこと")
        with wave.open(str(path), "rb") as w:
            if w.getframerate() != index["sample_rate"] or w.getnchannels() != 1:
                sys.exit(f"{path.name}: {index['sample_rate']}Hz mono ではない")
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        clips.append({**clip, "pcm": pcm, "seconds": pcm.size / index["sample_rate"]})
    return clips


def linear_fit(xs: list[float], ys: list[float]) -> dict | None:
    """y = a + b*x の最小二乗解。a=固定費(s), b=限界コスト(s/audio_s)。

    a と b を分けて見るのは、発話を細かく割る変更（#27 の turn 連結や partial 字幕）が
    固定費を何回払うかを変えるため。R² が低ければ直線モデル自体が疑わしい。
    """
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    return {
        "fixed_s": round(a, 3),
        "marginal_s_per_audio_s": round(b, 4),
        "r2": round(1 - ss_res / ss_tot, 3) if ss_tot else None,
        "n": n,
    }


def _summarize_counts(counts: list[EditCounts]) -> dict:
    total = EditCounts()
    for c in counts:
        total = total + c
    rate = total.error_rate
    return {
        "rate": round(rate, 4) if rate is not None else None,
        "substitutions": total.substitutions,
        "deletions": total.deletions,
        "insertions": total.insertions,
        "ref_len": total.ref_len,
    }


def _rate(counts: list[EditCounts]) -> float | None:
    rate = corpus_rate(counts)
    return round(rate, 4) if rate is not None else None


def phase_asr_ext(
    model_key: str,
    models_dir: Path,
    tokenizer_name: str = "charclass",
    label: str | None = None,
    **sherpa_kwargs,
) -> dict:
    """拡張コーパスでの CER/WER/RTF と decode の固定費/限界コスト分離。

    既存の asr:* フェーズ（10文固定・速度のみ）は判断ゲート①との比較用に残してあり、
    こちらが #24 のベースライン本体。#28 で sherpa-onnx 系も同じ土俵に載せた
    （`label` は同一モデルの設定違いを並べるための表示名）。
    """
    tokenizer_name, tokenize = resolve_tokenizer(tokenizer_name)
    clips = load_ext_clips()
    before = rss_mb()
    transcribe, load_s = make_transcriber(model_key, models_dir, **sherpa_kwargs)

    transcribe(clips[0]["pcm"].astype(np.float32) / 32768.0)  # ウォームアップ
    peak_rss = rss_mb()
    items = []
    for clip in clips:
        text, dt = transcribe(clip["pcm"].astype(np.float32) / 32768.0)
        text = text.strip()
        peak_rss = max(peak_rss, rss_mb())
        cer = cer_counts(clip["text"], text)
        cer_raw = cer_counts(clip["text"], text, drop_marks=False)
        wer = wer_counts(clip["text"], text, tokenize)
        items.append(
            {
                "id": clip["id"],
                "category": clip["category"],
                "audio_s": round(clip["seconds"], 2),
                "decode_s": round(dt, 3),
                "rtf": round(dt / clip["seconds"], 3),
                "ref": clip["text"],
                "hyp": text,
                "ref_chars": cer.ref_len,
                "cer": round(cer.error_rate, 4) if cer.error_rate is not None else None,
                "cer_raw": round(cer_raw.error_rate, 4) if cer_raw.error_rate is not None else None,
                "wer": round(wer.error_rate, 4) if wer.error_rate is not None else None,
                "_cer": cer,
                "_cer_raw": cer_raw,
                "_wer": wer,
            }
        )

    # 正解テキストが空のクリップ（noise-only）は誤り率が定義できないので別枠。
    # 無発話に何文字ねじ込んだか＝幻覚の量として見る
    scored = [i for i in items if i["ref_chars"] > 0]
    empty_ref = [i for i in items if i["ref_chars"] == 0]

    return {
        "model": model_key,
        "label": label or ASR_LABELS[model_key],
        "corpus": "ja_ext",
        "clips": len(items),
        "audio_s_total": round(sum(i["audio_s"] for i in items), 1),
        "wer_tokenizer": tokenizer_name,
        "load_s": round(load_s, 1),
        "rss_mb": round(rss_mb() - before),
        "peak_rss_mb": round(peak_rss),
        "cer": _summarize_counts([i["_cer"] for i in scored]),
        "cer_raw_rate": _rate([i["_cer_raw"] for i in scored]),
        "wer": _summarize_counts([i["_wer"] for i in scored]),
        # 速度は発話クリップだけで見る。無発話クリップ（noise-only）は出力が空でも
        # デコードに時間がかかり、混ぜると回帰も RTF も歪む（下の empty_reference 参照）
        "rtf_median": round(statistics.median(i["rtf"] for i in scored), 3),
        "rtf_max": round(max(i["rtf"] for i in scored), 3),
        "decode_s_median": round(statistics.median(i["decode_s"] for i in scored), 2),
        "cost_model": linear_fit(
            [i["audio_s"] for i in scored], [i["decode_s"] for i in scored]
        ),
        "by_category": {
            cat: {
                "clips": len([i for i in scored if i["category"] == cat]),
                "cer": _rate([i["_cer"] for i in scored if i["category"] == cat]),
                "wer": _rate([i["_wer"] for i in scored if i["category"] == cat]),
                "rtf_median": round(
                    statistics.median(i["rtf"] for i in scored if i["category"] == cat), 3
                ),
            }
            for cat in sorted({i["category"] for i in scored})
        },
        # 無発話クリップ: 誤り率は定義できない。幻覚の量（挿入文字数）と、
        # 「何も出力しなくてもデコード時間は払う」ことを示す decode_s を残す
        "empty_reference": [
            {
                "id": i["id"],
                "audio_s": i["audio_s"],
                "decode_s": i["decode_s"],
                "rtf": i["rtf"],
                "hallucinated_chars": i["_cer"].insertions,
                "hyp": i["hyp"],
            }
            for i in empty_ref
        ],
        "items": [{k: v for k, v in i.items() if not k.startswith("_")} for i in items],
    }


# ---- 区切り（#24 の基準線 / #27 の A-B 比較） ----


def segment_clip(segmenter, pcm: np.ndarray, frame_len: int) -> list[dict]:
    """1クリップをフレーム単位で流し込み、確定位置つきのイベント列を返す。

    確定位置を取るためにフレームを1枚ずつ渡す（feed はフレーム境界でしか判定しない）。
    endpoint latency = 確定位置 − 最後に音声を検出した位置。

    返すのは `Segment` と、morph 戦略でのみ現れる `TurnBoundary`（#27）。
    `closed_at_s` は音声先頭からの秒。**実時間ではない**ので、ASR の計算時間は
    含まない（A/B とも同じ条件なので比較には使える）。
    """
    from server.audio.vad import Segment

    segmenter.reset()
    out: list[dict] = []
    fed = 0
    for i in range(pcm.size // frame_len):
        frame = pcm[i * frame_len : (i + 1) * frame_len]
        fed += frame.size
        for event in segmenter.feed(frame):
            out.append(
                {
                    "seg": event if isinstance(event, Segment) else None,
                    "boundary": None if isinstance(event, Segment) else event,
                    "closed_at_s": fed / segmenter.sample_rate,
                    "flushed": False,
                }
            )
    tail = segmenter.flush()
    if tail is not None:
        out.append(
            {
                "seg": tail,
                "boundary": None,
                "closed_at_s": pcm.size / segmenter.sample_rate,
                "flushed": True,
            }
        )
    return out


def _gap_for_boundary(gaps: list[dict], prev_end_s: float, next_start_s: float) -> dict | None:
    """セグメント境界が乗っている注釈ギャップを返す（重なりで判定）。"""
    for gap in gaps:
        if prev_end_s <= gap["end_s"] and next_start_s >= gap["start_s"]:
            return gap
    return None


def build_segmenter(config, turn_config):
    """turn 戦略に対応した VoiceSegmenter を作る（pipeline と同じ組み立て方）。"""
    from server.audio.vad import VoiceSegmenter, build_frame_vad

    frame_vad = build_frame_vad(config.vad)
    segmenter = VoiceSegmenter(
        frame_vad,
        max_utterance_s=config.vad.max_utterance_s,
        frame_ms=frame_vad.frame_ms,
        pre_roll_ms=config.vad.pre_roll_ms,
        **turn_config.segmenter_kwargs(config.vad),
    )
    return segmenter, frame_vad


def phase_segmentation(
    asr_key: str | None, models_dir: Path, config, turn_config=None
) -> dict:
    """turn 戦略ごとの区切り品質（#24 の基準線 / #27 の A-B 比較）。

    採点対象は区切り注釈のあるクリップのみ（既存10文は boundary_annotated=false
    ＝ ギャップ注釈が無く、#22 の結論により採点外）。
    asr_key を与えると各 Segment を書き起こし、**字幕カード = Turn** の粒度で数える。

    turn_config を省略すると config.turn（既定 simple）を使う。simple では
    TurnBoundary が流れず 1 Segment = 1 Turn なので、#24 と同じ数値が出る。

    ASR なし（asr_key=None）では文法クラスを判定できないため、morph は
    「Segment は割るが Turn は TurnBoundary でしか確定しない」極端な連結になる。
    morph の評価には必ず ASR を付けること。
    """
    from server.turn import TurnAssembler, build_boundary_classifier
    from server.turn.assembler import TurnPart

    turn_config = turn_config or config.turn
    clips = [c for c in load_ext_clips() if c["boundary_annotated"]]
    segmenter, frame_vad = build_segmenter(config, turn_config)
    frame_len = segmenter.sample_rate * frame_vad.frame_ms // 1000

    model = None
    if asr_key is not None:
        # #28 で sherpa 系も同じフェーズに載せた。以降 `model` は
        # `transcribe(audio)->(text, seconds)` という呼び出し規約だけを満たす
        model, _load_s = make_transcriber(asr_key, models_dir)
        model(clips[0]["pcm"].astype(np.float32) / 32768.0)  # ウォームアップ

    per_clip: list[dict] = []
    endpoint_latencies: list[float] = []
    # #34: 取りこぼしの代償を数字で言うための指標。`endpoint_latency_ms` は音源終端で
    # 確定した Turn を数えていないので、**文法で確定できなかった Turn の待ちが指標に
    # 出てこない**（#27 の申し送り）。音源はそこで終わるが、実授業では無音が続くので
    # その Turn は force_silence_ms 待たされる。**確定理由から実待ちを引き当てる**:
    #   strong_end / predicate_end / segment … Segment が閉じた時点 = min_silence_ms
    #   boundary / flush                   … 文法が確定させられず timeout = force_silence_ms
    turn_waits: list[float] = []
    timeout_turns = 0
    _kwargs = turn_config.segmenter_kwargs(config.vad)
    _force_ms = _kwargs["force_silence_ms"]
    force_silence_s = None if _force_ms is None else _force_ms / 1000.0
    min_silence_s = _kwargs["min_silence_ms"] / 1000.0
    # 文法ではなく無音の経過で確定した Turn（= 取りこぼし）
    TIMEOUT_REASONS = ("boundary", "flush")
    card_chars: list[int] = []
    unnatural = 0
    natural_hits = 0
    natural_total = 0
    asr_calls = 0
    decode_s_total = 0.0
    merged_turns = 0
    reasons: Counter[str] = Counter()

    for clip in clips:
        assembler = TurnAssembler(
            build_boundary_classifier(turn_config.classifier),
            strategy=turn_config.strategy,
            max_turn_s=turn_config.morph.max_turn_s,
            max_segments=turn_config.morph.max_segments,
        )
        cards: list[dict] = []

        def emit(turn, at_s: float, *, counted: bool = True) -> None:
            """確定した Turn を1枚のカードとして記録する。

            counted=False は「無音待ちで確定したのではない」ケース（音源の終端）。
            #24 と同じ定義を保つため endpoint latency には数えない。
            """
            nonlocal merged_turns, timeout_turns
            latency_s = at_s - turn.t_end
            if counted:
                endpoint_latencies.append(latency_s)
            # 実授業での待ち（#34）。音源終端で切れた Turn は計測値が短く出るので、
            # 確定理由に対応する無音長を代わりに計上する
            if turn.reason in TIMEOUT_REASONS:
                timeout_turns += 1
                wait_s = force_silence_s if force_silence_s is not None else latency_s
            elif counted:
                wait_s = latency_s
            else:
                wait_s = min_silence_s
            turn_waits.append(wait_s)
            card = {
                "t_start": round(turn.t_start, 2),
                "t_end": round(turn.t_end, 2),
                "closed_at_s": round(at_s, 2),
                "endpoint_latency_ms": round(latency_s * 1000),
                "wait_ms": round(wait_s * 1000),  # #34: 実授業での実待ち相当
                "segments": turn.parts,
                "reason": turn.reason,
            }
            if model is not None:
                card["text"] = turn.text
                card["chars"] = len(normalize_ja(turn.text))
                card_chars.append(card["chars"])
            cards.append(card)
            reasons[turn.reason] += 1
            if turn.merged:
                merged_turns += 1

        for entry in segment_clip(segmenter, clip["pcm"], frame_len):
            if entry["boundary"] is not None:
                turn = assembler.on_boundary()
                if turn is not None:
                    emit(turn, entry["boundary"].at)
                continue
            seg = entry["seg"]
            text = ""
            if model is not None:
                text, decode_s = model(seg.pcm.astype(np.float32) / 32768.0)
                text = text.strip()
                decode_s_total += decode_s
            asr_calls += 1
            turn = assembler.add_segment(
                TurnPart(
                    text=text,
                    t_start=seg.t_start,
                    t_end=seg.t_end,
                    asr_ms=0,
                    closed_at=entry["closed_at_s"],
                    audio_s=seg.pcm.size / segmenter.sample_rate,
                )
            )
            if turn is not None:
                emit(turn, entry["closed_at_s"], counted=not entry["flushed"])
        tail = assembler.flush()
        if tail is not None:
            emit(tail, clip["seconds"], counted=False)

        clip_unnatural = 0
        for prev, nxt in zip(cards, cards[1:]):
            gap = _gap_for_boundary(clip["gaps"], prev["t_end"], nxt["t_start"])
            if gap is None or not gap["natural_break"]:
                # 注釈の無い位置での分割も「文の途中で割った」扱い
                # （正解の切れ目は gaps[] に全部載っている）
                clip_unnatural += 1
            else:
                natural_hits += 1
        unnatural += clip_unnatural
        natural_total += sum(1 for g in clip["gaps"] if g["natural_break"])

        per_clip.append(
            {
                "id": clip["id"],
                "category": clip["category"],
                "audio_s": round(clip["seconds"], 2),
                "expected_turns": clip["expected_turns"],
                "cards": len(cards),
                "unnatural_splits": clip_unnatural,
                "detail": cards,
            }
        )

    total_cards = sum(c["cards"] for c in per_clip)
    expected = sum(c["expected_turns"] or 0 for c in per_clip)
    return {
        "vad": config.vad.engine,
        "strategy": turn_config.strategy,
        "classifier": turn_config.classifier,  # #34 の A/B はここで before/after が分かれる
        "min_silence_ms": turn_config.segmenter_kwargs(config.vad)["min_silence_ms"],
        "start_speech_ms": turn_config.segmenter_kwargs(config.vad)["start_speech_ms"],
        "force_silence_ms": turn_config.segmenter_kwargs(config.vad)["force_silence_ms"],
        "frame_ms": frame_vad.frame_ms,
        "asr": asr_key,
        "clips": len(clips),
        "audio_s_total": round(sum(c["seconds"] for c in clips), 1),
        "cards": total_cards,
        "expected_turns": expected,
        "cards_per_expected_turn": round(total_cards / expected, 2) if expected else None,
        "unnatural_splits": unnatural,
        "natural_breaks_total": natural_total,
        "natural_breaks_hit": natural_hits,
        "merged_turns": merged_turns,
        "turn_reasons": dict(reasons),
        # ASR は Segment 単位。#24 の「固定費が支配的」を踏まえ、CPU 側の代償を必ず出す
        "asr_calls": asr_calls,
        "asr_decode_s_total": round(decode_s_total, 2) if model is not None else None,
        "endpoint_latency_ms_median": (
            round(statistics.median(endpoint_latencies) * 1000) if endpoint_latencies else None
        ),
        "endpoint_latency_ms_max": (
            round(max(endpoint_latencies) * 1000) if endpoint_latencies else None
        ),
        # #34: 文法で確定できず無音の経過で確定した Turn（= 取りこぼし）と、その待ちの合計。
        # `endpoint_latency_ms_*` は定義を変えていない（#24/#27/#28 との比較可能性のため）
        "timeout_turns": timeout_turns,
        "timeout_wait_ms_total": (
            round(timeout_turns * force_silence_s * 1000) if force_silence_s is not None else 0
        ),
        "turn_wait_ms_median": (
            round(statistics.median(turn_waits) * 1000) if turn_waits else None
        ),
        "turn_wait_ms_max": round(max(turn_waits) * 1000) if turn_waits else None,
        "card_chars_mean": round(statistics.mean(card_chars), 1) if card_chars else None,
        "card_chars_median": round(statistics.median(card_chars)) if card_chars else None,
        # 生徒が全言語を使っている前提の呼び出し回数（1発話 × 提供言語数）
        "mt_calls": total_cards * len(config.languages),
        "langs": len(config.languages),
        "per_clip": per_clip,
    }


# ---- Hy-MT2 の使い方の最適化（#26） ----

HYMT_JA_LABEL = "Japanese"  # 往復翻訳（戻し）用のプロンプト言語名

# サンプリングの振れを見るための反復数。1文×1言語あたりの推論回数を決めるので、
# 増やすとフェーズ全体が線形に伸びる（10文 × 2言語 × この回数）
SAMPLING_RUNS = 3


def char_disagreement(a: str, b: str) -> float:
    """2つの訳文の文字レベルの不一致率（0=完全一致）。

    参照訳が無いので「正しさ」は測れない。**同じ入力に対する出力のばらつき**と
    **貪欲とサンプリングの差の大きさ**を数値化するためだけに使う。
    正規化は CER と同じ（NFKC・空白/記号除去）なので、英語の大文字小文字と
    中国語の全角記号の差は数えない。
    """
    rate = cer_counts(a, b).error_rate
    return 0.0 if rate is None else rate


def phase_mt_decoding(models_dir: Path, config) -> dict:
    """B-5: サンプリング推論 vs 貪欲デコードの実測比較（#26）。

    参照訳が無いため、品質は **往復翻訳**（訳文を貪欲で日本語へ戻し、原文との CER）
    を代理指標にする。これは絶対品質の指標ではない。「貪欲がサンプリングより
    悪くないか」の相対判定にだけ使い、エンジン間比較（#28）には使わないこと。
    戻し翻訳は常に貪欲で行う（戻し側の乱数で判定が揺れないようにする）。

    **計測順のバイアスに注意**。llama.cpp は直前の入力とのプロンプト共通接頭辞を
    KVキャッシュから再利用するので、同じプロンプトの1回目だけがプロンプト評価を
    まるごと払う。素直に「貪欲→サンプリング」の順で測ると貪欲だけがその費用を被る
    （初回計測ではこれで貪欲が +15% 遅く見えていた）。ここでは
    (1) 捨て打ち1回でプロンプトを温めてから測る (2) モードの順序を文ごとに入れ替える、
    の2つで潰している。

    **この遅延はプロンプトが温まった状態の値**で、`mt:hy-mt2` フェーズの数値とは
    比較できない。ここで見るのは貪欲とサンプリングの**差**だけ。
    """
    sentences = load_sentences()
    llm = load_hymt(models_dir, config)
    hymt_chat(llm, sentences[0], HYMT_TARGETS["en"], temperature=0.0)  # ウォームアップ（計測外）

    def timed(text: str, label: str, temperature: float) -> tuple[str, float]:
        t = time.perf_counter()
        out = hymt_chat(llm, text, label, temperature=temperature)
        return out, (time.perf_counter() - t) * 1000

    def repeat(text: str, label: str, temperature: float, n: int) -> tuple[list[str], list[float]]:
        pairs = [timed(text, label, temperature) for _ in range(n)]
        return [p[0] for p in pairs], [p[1] for p in pairs]

    items: list[dict] = []
    pairs = [(t, lg) for t in sentences for lg in ("en", "zh")]
    for index, (text, lang) in enumerate(pairs):
        label = HYMT_TARGETS[lang]
        hymt_chat(llm, text, label, temperature=0.0)  # 捨て打ち: プロンプトを温める
        if index % 2 == 0:  # 順序の影響が残らないよう文ごとに入れ替える
            greedies, greedy_ms = repeat(text, label, 0.0, 2)  # 2回 = 決定性の確認
            samples, sample_ms = repeat(text, label, 0.7, SAMPLING_RUNS)
        else:
            samples, sample_ms = repeat(text, label, 0.7, SAMPLING_RUNS)
            greedies, greedy_ms = repeat(text, label, 0.0, 2)
        greedy_1, greedy_2 = greedies

        # 往復: 訳文 → 日本語（貪欲）→ 原文との CER
        back_greedy = hymt_chat(llm, greedy_1, HYMT_JA_LABEL, temperature=0.0)
        back_samples = [hymt_chat(llm, out, HYMT_JA_LABEL, temperature=0.0) for out in samples]
        spread = (
            statistics.mean(
                char_disagreement(samples[a], samples[b])
                for a in range(len(samples))
                for b in range(a + 1, len(samples))
            )
            if len(samples) > 1
            else 0.0
        )
        items.append({
            "ja": text,
            "lang": lang,
            "greedy": greedy_1,
            "greedy_repeat": greedy_2,
            "greedy_deterministic": greedy_1 == greedy_2,
            "greedy_ms": [round(m) for m in greedy_ms],
            "greedy_chars": len(greedy_1),
            "samples": samples,
            "sample_ms": [round(m) for m in sample_ms],
            "sample_chars": [len(o) for o in samples],
            "roundtrip_greedy": back_greedy,
            "roundtrip_samples": back_samples,
            "cer_roundtrip_greedy": round(cer_counts(text, back_greedy).error_rate or 0.0, 4),
            "cer_roundtrip_samples": [
                round(cer_counts(text, b).error_rate or 0.0, 4) for b in back_samples
            ],
            # サンプル同士のばらつき（0 = 3回とも同じ訳）
            "sample_spread": round(spread, 4),
            # 貪欲とサンプリングの差の大きさ（0 = 同じ訳が出ている）
            "greedy_vs_sampling": round(
                statistics.mean(char_disagreement(greedy_1, o) for o in samples), 4
            ),
        })

    def collect(key: str, lang: str) -> list[float]:
        """指定言語の値を並べる。値がリストなら展開する（1項目に複数回ぶんある指標用）。"""
        values: list[float] = []
        for item in items:
            if item["lang"] != lang:
                continue
            value = item[key]
            values.extend(value if isinstance(value, list) else [value])
        return values

    def agg_cer(kind: str) -> float:
        """コーパス全体の往復CER（クリップごとの平均でなく編集距離の総和で取る）。"""
        counts = []
        for i in items:
            backs = [i["roundtrip_greedy"]] if kind == "greedy" else i["roundtrip_samples"]
            counts.extend(cer_counts(i["ja"], b) for b in backs)
        return round(corpus_rate(counts) or 0.0, 4)

    def summary(ms_key: str, chars_key: str, deterministic: float, cer: float) -> dict:
        return {
            "ms_median": {
                lang: round(statistics.median(collect(ms_key, lang))) for lang in ("en", "zh")
            },
            "ms_max": {lang: round(max(collect(ms_key, lang))) for lang in ("en", "zh")},
            # 出力文字数。遅延差が「訳が長いから」なのかを切り分けるために出す
            "chars_median": {
                lang: round(statistics.median(collect(chars_key, lang))) for lang in ("en", "zh")
            },
            "deterministic_rate": round(deterministic, 3),
            "cer_roundtrip": cer,
        }

    return {
        "sentences": len(sentences),
        "sampling_runs": SAMPLING_RUNS,
        "greedy": summary(
            "greedy_ms",
            "greedy_chars",
            sum(1 for i in items if i["greedy_deterministic"]) / len(items),
            agg_cer("greedy"),
        ),
        "sampling": summary(
            "sample_ms",
            "sample_chars",
            # 3回とも同じ訳になった (文, 言語) の割合。1.0 なら実質決定的
            sum(1 for i in items if len(set(i["samples"])) == 1) / len(items),
            agg_cer("sampling"),
        ),
        "sample_spread_mean": round(statistics.mean(i["sample_spread"] for i in items), 4),
        "greedy_vs_sampling_mean": round(
            statistics.mean(i["greedy_vs_sampling"] for i in items), 4
        ),
        "items": items,
    }


# ---- 句読点の有無が翻訳品質に与える影響（#28 / #21 からの必須申し送り） ----

_PUNCT_RE = re.compile(r"[。、！？]")


def phase_mt_punct(models_dir: Path, config) -> dict:
    """**句読点だけ**を変数にして Hy-MT2 の訳文がどれだけ変わるかを測る。

    ReazonSpeech は句読点を出力できない（#21: 語彙5,224に `。！？` が無い）。
    ASR の CER が下がっても訳文が崩れるなら採用できないので、これが判定の決定打になる。

    実験の作りで大事なのは**入力の差を句読点だけに限定する**こと。実際の ASR 出力どうしを
    比べると誤認識の差が混ざって句読点の寄与が取り出せないので、**正解テキスト**を
    「句読点あり」と「句読点を抜いたもの」の2系統にして同じ文を2回翻訳する。

    #26 B-5 の教訓により**往復翻訳CERは使わない**（指標のノイズが判定したい差より大きい）。
    ここで出すのは (a) 訳文が完全一致した割合 (b) 変化したものの不一致率 (c) 全対訳の並び。
    **(c) は人が読んで劣化かどうかを判定するためのもので、数値だけでは採否を決めない。**
    """
    clips = load_ext_clips()
    seen: set[str] = set()
    sentences = []
    for clip in clips:
        text = clip["text"].strip()
        if text and text not in seen and _PUNCT_RE.search(text):
            seen.add(text)
            sentences.append(text)

    llm = load_hymt(models_dir, config)
    temperature = config.mt.hy_mt2.temperature
    items = []
    for text in sentences:
        stripped = _PUNCT_RE.sub("", text)
        for lang, label in HYMT_TARGETS.items():
            t0 = time.perf_counter()
            with_punct = hymt_chat(llm, text, label, temperature=temperature)
            dt_with = time.perf_counter() - t0
            t0 = time.perf_counter()
            without = hymt_chat(llm, stripped, label, temperature=temperature)
            dt_without = time.perf_counter() - t0
            items.append(
                {
                    "ja": text,
                    "ja_stripped": stripped,
                    "lang": lang,
                    "with_punct": with_punct,
                    "without_punct": without,
                    "identical": with_punct == without,
                    "disagreement": round(char_disagreement(with_punct, without), 4),
                    "latency_with_s": round(dt_with, 3),
                    "latency_without_s": round(dt_without, 3),
                }
            )

    by_lang = {}
    for lang in HYMT_TARGETS:
        rows = [i for i in items if i["lang"] == lang]
        changed = [i for i in rows if not i["identical"]]
        by_lang[lang] = {
            "pairs": len(rows),
            "identical": len(rows) - len(changed),
            "identical_rate": round((len(rows) - len(changed)) / len(rows), 3) if rows else None,
            # 変化したペアだけの不一致率。全体平均だと一致ペアに薄められて意味が消える
            "disagreement_median_when_changed": (
                round(statistics.median(i["disagreement"] for i in changed), 4) if changed else None
            ),
            "disagreement_max": round(max((i["disagreement"] for i in rows), default=0.0), 4),
        }
    return {
        "sentences": len(sentences),
        "temperature": temperature,
        "by_lang": by_lang,
        "items": items,
    }


def phase_threads(asr_threads: int, mt_threads: int, models_dir: Path, config) -> dict:
    """B-6: ASR（CTranslate2）と MT（llama.cpp）のスレッド配分を競合下で測る（#26）。

    `asr_threads=0` は `cpu_threads` 未指定＝ライブラリ既定（全コア）で、現状の挙動。
    application worker 数（各1）は変えない。振るのは推論ライブラリの内部スレッド数だけ。
    """
    from faster_whisper import WhisperModel

    wavs = load_wavs()
    sentences = load_sentences()
    asr = WhisperModel(
        str(models_dir / ASR_MODELS["small"]),
        device="cpu",
        compute_type="int8",
        cpu_threads=asr_threads,  # 0 = ライブラリ既定
    )
    translate = make_hymt(models_dir, config, n_threads=mt_threads)

    timed_transcribe(asr, wavs[0]["audio"])  # ウォームアップ
    translate(sentences[0], "en")

    peak = {"rss": rss_mb()}
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            peak["rss"] = max(peak["rss"], rss_mb())
            time.sleep(0.1)

    asr_times: list[dict] = []
    mt_times: list[float] = []

    def asr_worker() -> None:
        for w in wavs:
            _, dt = timed_transcribe(asr, w["audio"])
            asr_times.append({"audio_s": w["seconds"], "decode_s": dt})

    def mt_worker() -> None:
        for text in sentences:
            for lang in ("en", "zh"):
                t = time.perf_counter()
                translate(text, lang)
                mt_times.append((time.perf_counter() - t) * 1000)

    workers = [threading.Thread(target=asr_worker), threading.Thread(target=mt_worker)]
    sampler_thread = threading.Thread(target=sampler, daemon=True)
    t0 = time.perf_counter()
    for th in workers:
        th.start()
    sampler_thread.start()
    for th in workers:
        th.join()
    stop.set()
    wall = time.perf_counter() - t0

    asr_median = statistics.median(x["decode_s"] for x in asr_times)
    mt_median = statistics.median(mt_times)
    return {
        "asr_threads": asr_threads,
        "mt_threads": mt_threads,
        "wall_s": round(wall, 1),
        "peak_rss_mb": round(peak["rss"]),
        "asr_decode_s_median": round(asr_median, 2),
        "asr_decode_s_max": round(max(x["decode_s"] for x in asr_times), 2),
        "mt_ms_median": round(mt_median),
        "mt_ms_max": round(max(mt_times)),
        # 「発話終了→2言語目のcaption」の競合下見積り（build_report の estimate と同じ組み方）
        "pipeline_s": round(asr_median + 2 * mt_median / 1000, 2),
        # 同じ組み方の最悪値。中央値だけで選ぶと、ASRの尾を犠牲にして
        # 中央値を稼ぐ組合せを選んでしまう（N-01 は中央値と最大の両方が基準）
        "pipeline_tail_s": round(
            max(x["decode_s"] for x in asr_times) + 2 * max(mt_times) / 1000, 2
        ),
    }


def _turn(strategy: str, classifier: str = "surface", **morph):
    """A/B 用の TurnConfig。config.yaml を書き換えずに戦略だけ差し替える。

    `classifier` は #34 の A/B 用。`surface-polite-only` が #34 以前の分類器。
    """
    from server.config import TurnConfig, TurnMorphConfig

    return TurnConfig(strategy=strategy, classifier=classifier, morph=TurnMorphConfig(**morph))


PHASES = {
    "asr:kotoba": lambda d, c: phase_asr("kotoba", d),
    "asr:small": lambda d, c: phase_asr("small", d),
    "mt:nllb": lambda d, c: phase_mt("nllb", d, c),
    "mt:hy-mt2": lambda d, c: phase_mt("hy-mt2", d, c),
    # 同時実行は全組合せ計測（既定構成の競合下の数値も判断材料にする）
    "concurrent:kotoba:nllb": lambda d, c: phase_concurrent("kotoba", "nllb", d, c),
    "concurrent:kotoba:hy-mt2": lambda d, c: phase_concurrent("kotoba", "hy-mt2", d, c),
    "concurrent:small:nllb": lambda d, c: phase_concurrent("small", "nllb", d, c),
    "concurrent:small:hy-mt2": lambda d, c: phase_concurrent("small", "hy-mt2", d, c),
    # #24 ベースライン。既定の実行対象には入れない（判断ゲート①の出力を変えないため）
    "asr-ext:kotoba": lambda d, c: phase_asr_ext("kotoba", d),
    "asr-ext:small": lambda d, c: phase_asr_ext("small", d),
    # #28 判断ゲート②。reazon の派生は設定の効き目を分離するためのもの。
    # nolead は lead_in_ms=0（冒頭の取りこぼしがどれだけ効くかの対照群）
    # asr-ext:reazon は**出荷既定と同じ構成**（beam + lead 600ms）。他は要因分離用の対照群
    "asr-ext:reazon": lambda d, c: phase_asr_ext(
        "reazon", d, label="ReazonSpeech (既定: beam + lead600)",
        decoding_method="modified_beam_search",
    ),
    "asr-ext:reazon-greedy": lambda d, c: phase_asr_ext(
        "reazon", d, label="ReazonSpeech (greedy + lead600)"
    ),
    "asr-ext:reazon-nolead": lambda d, c: phase_asr_ext(
        "reazon", d, label="ReazonSpeech (lead なし)", lead_in_ms=0
    ),
    "asr-ext:reazon-lead300": lambda d, c: phase_asr_ext(
        "reazon", d, label="ReazonSpeech (lead 300ms)", lead_in_ms=300
    ),
    "asr-ext:reazon-lead1000": lambda d, c: phase_asr_ext(
        "reazon", d, label="ReazonSpeech (lead 1000ms)", lead_in_ms=1000
    ),
    "asr-ext:reazon-int8": lambda d, c: phase_asr_ext(
        "reazon", d, label="ReazonSpeech (全int8)", int8_decoder=True
    ),
    "asr-ext:reazon-hotwords": lambda d, c: phase_asr_ext(
        "reazon", d, label="ReazonSpeech (beam + 教科用語 hotwords)",
        decoding_method="modified_beam_search",
        hotwords_file=ROOT / "tests" / "fixtures" / "ja_hotwords.txt",
    ),
    "segmentation:vad": lambda d, c: phase_segmentation(None, d, c),
    "segmentation:small": lambda d, c: phase_segmentation("small", d, c),
    # #27 turn 連結。simple は #24 の基準線と同じ数値が出る（回帰チェックを兼ねる）。
    # morph の既定は check500/start96/force1000（実測の勝者）。c320 系は Parapper の
    # 実測既定で、force_silence_ms の掃引つき。nostart は start_speech_ms の分離用
    "turn:simple:small": lambda d, c: phase_segmentation("small", d, c, _turn("simple")),
    "turn:morph:small": lambda d, c: phase_segmentation("small", d, c, _turn("morph")),
    "turn:morph-c320:small": lambda d, c: phase_segmentation(
        "small", d, c, _turn("morph", check_silence_ms=320, force_silence_ms=640)
    ),
    "turn:morph-c320-f800:small": lambda d, c: phase_segmentation(
        "small", d, c, _turn("morph", check_silence_ms=320, force_silence_ms=800)
    ),
    "turn:morph-c320-f1000:small": lambda d, c: phase_segmentation(
        "small", d, c, _turn("morph", check_silence_ms=320, force_silence_ms=1000)
    ),
    "turn:morph-nostart:small": lambda d, c: phase_segmentation(
        "small", d, c, _turn("morph", start_speech_ms=0)
    ),
    # #28: ReazonSpeech は句読点を出さない（#21）。`classifier: surface` の StrongEnd が
    # 消えたときに PredicateEnd だけで区切りが保てるかを、#27 と同じ土俵で測る
    "turn:simple:reazon": lambda d, c: phase_segmentation("reazon", d, c, _turn("simple")),
    "turn:morph:reazon": lambda d, c: phase_segmentation("reazon", d, c, _turn("morph")),
    # #34 の A/B。before は #34 以前の分類器（敬体しか言い切りと認めない）。
    # after は出荷既定の `turn:morph:reazon` そのもの — 別フェーズを作らないのは、
    # 「既定の構成でそのまま良くなった」ことを同じ数値で示すため
    "turn:morph-politeonly:reazon": lambda d, c: phase_segmentation(
        "reazon", d, c, _turn("morph", classifier="surface-polite-only")
    ),
    # #26 Hy-MT2 の使い方の最適化。threads:<ASR>x<MT> の 0 は cpu_threads 未指定（現状）
    "mt-decoding:hy-mt2": lambda d, c: phase_mt_decoding(d, c),
    # #28: 句読点の有無だけを変えて Hy-MT2 の訳文の変化を見る
    "mt-punct:hy-mt2": lambda d, c: phase_mt_punct(d, c),
    "threads:0x4": lambda d, c: phase_threads(0, 4, d, c),
    "threads:8x4": lambda d, c: phase_threads(8, 4, d, c),
    "threads:6x4": lambda d, c: phase_threads(6, 4, d, c),
    "threads:4x4": lambda d, c: phase_threads(4, 4, d, c),
    "threads:4x8": lambda d, c: phase_threads(4, 8, d, c),
    "threads:6x8": lambda d, c: phase_threads(6, 8, d, c),
    "threads:8x8": lambda d, c: phase_threads(8, 8, d, c),
    "threads:4x12": lambda d, c: phase_threads(4, 12, d, c),
}

# `python scripts/bench.py`（引数なし）が走らせるフェーズ。判断ゲート①（2026-07-07）の
# レポートを同じ形で再生成できるよう、当時の8フェーズに固定する
DEFAULT_PHASES = [
    "asr:kotoba",
    "asr:small",
    "mt:nllb",
    "mt:hy-mt2",
    "concurrent:kotoba:nllb",
    "concurrent:kotoba:hy-mt2",
    "concurrent:small:nllb",
    "concurrent:small:hy-mt2",
]


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
    # 発話長はASRフェーズの実測メタから取る（--report-from をフィクスチャ非依存にするため）
    asr_any = next((results[k] for k in ("asr:small", "asr:kotoba") if results.get(k)), None)
    if asr_any is None:
        sys.exit("ASRフェーズの結果が無いため、レポートを構成できない")
    median_utt = statistics.median(i["audio_s"] for i in asr_any["items"])

    def estimate(asr: dict | None, mt: dict | None, conc: dict | None) -> dict | None:
        """発話終了→2言語目のcaption送出までの見積り（ASR→en→zh直列、plan.md §6.3の構成）。"""
        if not asr or not mt:
            return None
        med = asr["rtf_median"] * median_utt + (mt["ms_median"]["en"] + mt["ms_median"]["zh"]) / 1000
        worst = asr["rtf_max"] * median_utt + (mt["ms_max"]["en"] + mt["ms_max"]["zh"]) / 1000
        out = {"median_s": round(med, 1), "worst_s": round(worst, 1),
               "pass_median": med <= BUDGET_MEDIAN_S, "pass_max": worst <= BUDGET_MAX_S}
        if conc:  # 競合下（ASR・MTが同時に飽和した瞬間）の参考値
            out["contended_s"] = round(
                conc["asr_decode_s_median"] + 2 * conc["mt_ms_median"] / 1000, 1
            )
        return out

    est = {
        (a, m): estimate(
            results.get(f"asr:{a}"), results.get(f"mt:{m}"), results.get(f"concurrent:{a}:{m}")
        )
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
    add("## (c) ASR＋翻訳 同時実行（全組合せ）")
    add("")
    add("| ASR | 翻訳 | 完走 | ピークRSS | ASRデコード中央値(競合下) | 翻訳中央値(競合下) | 翻訳最大(競合下) |")
    add("|-----|------|------|----------|-------------------------|------------------|----------------|")
    for a in ("kotoba", "small"):
        for m in ("nllb", "hy-mt2"):
            r = results.get(f"concurrent:{a}:{m}")
            if r:
                add(f"| {a} | {m} | {r['wall_s']}s | {r['peak_rss_mb']}MB | {r['asr_decode_s_median']}s | {r['mt_ms_median']}ms | {r['mt_ms_max']}ms |")
            else:
                add(f"| {a} | {m} | 計測失敗 | - | - | - | - |")
    add("")
    add(f"## 遅延バジェット判定（発話終了→2言語目caption、中央値≤{BUDGET_MEDIAN_S:.0f}s / 最大≤{BUDGET_MAX_S:.0f}s）")
    add("")
    add(f"発話 {median_utt:.1f}s（fixture中央値）を ASR→en→zh 直列処理した場合の見積り。")
    add("合否は非競合値（a)(b)で判定し、競合下（ASRとMTが同時飽和した瞬間）の参考値を併記する:")
    add("")
    add("| 構成 | 中央値見積り | 競合下参考値 | 最悪見積り | 中央値≤5s | 最大≤8s |")
    add("|------|------------|------------|-----------|----------|--------|")
    for (a, m), e in est.items():
        if e:
            cont = f"{e['contended_s']}s" if "contended_s" in e else "-"
            add(f"| {a} + {m} | {e['median_s']}s | {cont} | {e['worst_s']}s | {'✅' if e['pass_median'] else '❌'} | {'✅' if e['pass_max'] else '❌'} |")
        else:
            add(f"| {a} + {m} | 計測失敗 | - | - | - | - |")
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
        add(f"- **既定ASRモデル = {ASR_LABELS[chosen[0]]}** / **既定翻訳エンジン = {chosen[1]}**")
        add("  （品質優先の選好順 kotoba+hy-mt2 → kotoba+nllb → small+hy-mt2 → small+nllb で、予算を満たす最初の構成）")
    else:
        add("- **予算を満たす構成なし** → plan.md R-01 に従い構成再検討（Opus-MT等の追加調査）")
    add("- この結論を config.yaml の既定値へ反映する。**学校の実機（i5）での再計測が済むまで暫定確定**")
    add("")
    add("### 所見（数値に表れないトレードオフ）")
    add("")
    kotoba_asr = results.get("asr:kotoba")
    if kotoba_asr:
        add(f"- **kotoba はCPUでは不採用**: デコード時間が発話長によらずほぼ一定（中央値 {kotoba_asr['decode_s_median']}s）で、"
            f"それ単独で中央値予算 {BUDGET_MEDIAN_S:.0f}s を圧迫する。精度は良いため、GPU化や録画の事後書き起こし用途では有力")
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
    parser.add_argument(
        "--list-phases", action="store_true", help="実行可能なフェーズ名を並べて終了"
    )
    parser.add_argument("--models-dir", default=None)
    parser.add_argument("--out-dir", default=str(ROOT / "docs" / "bench"))
    parser.add_argument("--report-from", default=None, help="既存の生JSONからレポートのみ再生成")
    args = parser.parse_args()

    if args.list_phases:
        for name in sorted(PHASES):
            mark = "*" if name in DEFAULT_PHASES else " "
            print(f"{mark} {name}")
        print("\n* = 引数なし実行の対象（判断ゲート①の8フェーズ）")
        return 0

    config = load_config(ROOT / "config.yaml")
    models_dir = Path(args.models_dir) if args.models_dir else config.models.resolved_dir

    if args.phase:
        result = PHASES[args.phase](models_dir, config)
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
    for phase in DEFAULT_PHASES:
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
