"""授業の録画・録音で ASR 設定を比べる（docs/bench/2026-10-25-lesson-asr.md）。

製品と同じ VoiceSegmenter（config.yaml の vad / turn）で Segment に切り、各 Segment を
ReazonSpeech で書き起こす。`--ref` に Segment 番号→正解文の JSON を渡すと CER と
教科用語の再現数を出す。正解文が無い初回は `--out` の書き起こしを手で直して参照にする。

使い方:
  python scripts/lesson_asr_eval.py lesson.mp4 --out hyp.json
  python scripts/lesson_asr_eval.py lesson.mp4 --ref ref.json --terms server/asr/hotwords/geography.txt
  python scripts/lesson_asr_eval.py lesson.mp4 --ref ref.json --hotwords none --blank-penalty 0

授業の書き起こしは教材の複製になるので、参照・出力はリポジトリへ入れないこと。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.bench import build_segmenter, segment_clip  # noqa: E402
from scripts.text_metrics import cer_counts, corpus_rate  # noqa: E402
from server.asr.sherpa_engine import SherpaOnnxEngine  # noqa: E402
from server.config import load_config  # noqa: E402

SAMPLE_RATE = 16000


def load_audio(path: Path) -> np.ndarray:
    from faster_whisper import decode_audio  # PyAV 経由で mp4 も読める

    audio = decode_audio(str(path), sampling_rate=SAMPLE_RATE)
    return (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)


def segments(config, pcm: np.ndarray) -> list[np.ndarray]:
    segmenter, frame_vad = build_segmenter(config, config.turn)
    events = segment_clip(segmenter, pcm, SAMPLE_RATE * frame_vad.frame_ms // 1000)
    return [e["seg"].pcm for e in events if e["seg"] is not None]


def score(ref: dict[str, str], hyp: dict[str, str], terms: list[str]) -> str:
    counts = [cer_counts(ref[k], hyp.get(k, "")) for k in ref if ref[k]]
    total = sum(counts[1:], counts[0])
    want = sum(ref[k].count(t) for k in ref for t in terms)
    got = sum(min(ref[k].count(t), hyp.get(k, "").count(t)) for k in ref for t in terms)
    return (
        f"CER={corpus_rate(counts):.4f} S={total.substitutions} D={total.deletions} "
        f"I={total.insertions} N={total.ref_len} terms={got}/{want}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audio", type=Path, help="授業の動画または音声")
    parser.add_argument("--ref", type=Path, help="Segment 番号→正解文の JSON")
    parser.add_argument("--out", type=Path, help="書き起こしの保存先 JSON")
    parser.add_argument("--terms", type=Path, help="再現数を数える用語（1行1語）")
    parser.add_argument("--hotwords", default=None, help="既定は config.yaml の値。none で無効")
    parser.add_argument("--hotwords-score", type=float, default=None)
    parser.add_argument("--blank-penalty", type=float, default=None)
    args = parser.parse_args()

    config = load_config(ROOT / "config.yaml")
    sherpa = config.asr.sherpa
    if args.hotwords is not None:
        sherpa.hotwords_file = None if args.hotwords == "none" else args.hotwords
    engine = SherpaOnnxEngine(
        config.models.resolve(sherpa.model),
        num_threads=config.asr.cpu_threads,
        decoding_method=sherpa.decoding_method,
        lead_in_ms=sherpa.lead_in_ms,
        hotwords_file=sherpa.resolved_hotwords_file(),
        hotwords_score=sherpa.hotwords_score if args.hotwords_score is None else args.hotwords_score,
        blank_penalty=sherpa.blank_penalty if args.blank_penalty is None else args.blank_penalty,
    )
    engine.warmup()

    pcms = segments(config, load_audio(args.audio))
    started = time.perf_counter()
    hyp = {str(i): engine.transcribe(pcm, SAMPLE_RATE).text for i, pcm in enumerate(pcms)}
    elapsed = time.perf_counter() - started
    speech_s = sum(pcm.size for pcm in pcms) / SAMPLE_RATE
    print(f"segments={len(pcms)} speech={speech_s:.1f}s asr={elapsed:.1f}s")

    if args.out:
        args.out.write_text(json.dumps(hyp, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.ref:
        ref = json.loads(args.ref.read_text(encoding="utf-8"))
        if set(ref) != set(hyp):
            print("参照と Segment 数が合わない（vad / turn の設定が参照作成時と違う）", file=sys.stderr)
            return 1
        terms = args.terms.read_text(encoding="utf-8").split() if args.terms else []
        print(score(ref, hyp, terms))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
