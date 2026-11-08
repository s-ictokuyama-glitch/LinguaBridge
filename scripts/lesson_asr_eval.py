"""授業の録画・録音で ASR 設定を比べる（docs/bench/2026-10-25-lesson-asr.md）。

製品と同じ VoiceSegmenter で Segment に切り、config の asr.engine で書き起こす。
`--ref` に Segment 番号→正解文の JSON を渡すと CER と教科用語の再現数を出す。

使い方:
  python scripts/lesson_asr_eval.py lesson.mp4 --out ../evaluation/hyp.json
  python scripts/lesson_asr_eval.py lesson.mp4 --candidate-out ../evaluation/reazon.json
  python scripts/lesson_asr_eval.py lesson.mp4 --ref ref.json --terms server/asr/hotwords/geography.txt
  python scripts/lesson_asr_eval.py lesson.mp4 --ref ref.json --hotwords none --blank-penalty 0

授業の書き起こしは教材の複製になるので、参照・出力はリポジトリへ入れないこと。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field, TypeAdapter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.bench import build_segmenter, segment_clip  # noqa: E402
from scripts.text_metrics import EditCounts, cer_counts  # noqa: E402
from server.asr.base import ASREngine  # noqa: E402
from server.audio.vad import Segment  # noqa: E402
from server.config import AppConfig, SherpaConfig, load_config  # noqa: E402
from server.main import build_asr_engine  # noqa: E402

SAMPLE_RATE = 16000


class CandidateSegment(BaseModel):
    start_sample: int = Field(ge=0)
    end_sample: int = Field(gt=0)
    text: str


class CandidateRun(BaseModel):
    audio_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    segments: dict[str, CandidateSegment]


def audio_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def outside_repo(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"評価の生成物はリポジトリ外に保存してください。{path}")
    return resolved


def load_audio(path: Path) -> np.ndarray:
    from faster_whisper import decode_audio  # PyAV 経由で mp4 も読める

    audio = decode_audio(str(path), sampling_rate=SAMPLE_RATE)
    return (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)


def load_candidate_audio(path: Path) -> tuple[np.ndarray, str]:
    digest = audio_digest(path)
    pcm = load_audio(path)
    if audio_digest(path) != digest:
        raise ValueError("読み込み中に入力音声が変更されました")
    return pcm, digest


def segments(config: AppConfig, pcm: np.ndarray) -> list[Segment]:
    segmenter, frame_vad = build_segmenter(config, config.turn)
    events = segment_clip(segmenter, pcm, SAMPLE_RATE * frame_vad.frame_ms // 1000)
    return [e["seg"] for e in events if e["seg"] is not None]


def score(ref: dict[str, str], hyp: dict[str, str], terms: list[str]) -> str:
    counts = [cer_counts(text, hyp.get(index, "")) for index, text in ref.items()]
    total = sum(counts, EditCounts())
    want = sum(ref[k].count(t) for k in ref for t in terms)
    got = sum(min(ref[k].count(t), hyp.get(k, "").count(t)) for k in ref for t in terms)
    rate = "n/a" if total.error_rate is None else f"{total.error_rate:.4f}"
    return (
        f"CER={rate} S={total.substitutions} D={total.deletions} "
        f"I={total.insertions} N={total.ref_len} terms={got}/{want}"
    )


def evaluate(
    engine: ASREngine, speech: list[Segment]
) -> tuple[dict[str, CandidateSegment], float]:
    engine.warmup()
    results = {}
    elapsed = 0.0
    for index, segment in enumerate(speech):
        started = time.perf_counter()
        text = engine.transcribe(segment.pcm, SAMPLE_RATE).text
        elapsed += time.perf_counter() - started
        start = round(segment.t_start * SAMPLE_RATE)
        results[str(index)] = CandidateSegment(
            start_sample=start, end_sample=start + segment.pcm.size, text=text
        )
    return results, elapsed


def build_evaluation_engine(
    config: AppConfig, args: argparse.Namespace, stack: ExitStack
) -> ASREngine:
    sherpa_options = {
        "hotwords_score": args.hotwords_score,
        "blank_penalty": args.blank_penalty,
        "refill_gap_s": args.refill_gap,
    }
    overrides = {key: value for key, value in sherpa_options.items() if value is not None}
    if args.hotwords is not None:
        overrides["hotwords_file"] = None if args.hotwords == "none" else args.hotwords
    if overrides:
        if config.asr.engine != "sherpa":
            raise ValueError("--hotwords 等の ReazonSpeech 用引数には asr.engine: sherpa が必要です")
        config.asr.sherpa = SherpaConfig.model_validate(config.asr.sherpa.model_dump() | overrides)
    if config.asr.engine == "speech-cpp":
        from scripts.speech_cpp import SpeechCppEngine

        if args.speech_cpp_binary is None or args.speech_cpp_model is None:
            raise ValueError("speech-cpp には --speech-cpp-binary と --speech-cpp-model が必要です")
        keywords = (
            args.speech_cpp_keywords.read_text(encoding="utf-8-sig")
            if args.speech_cpp_keywords else None
        )
        return stack.enter_context(SpeechCppEngine(
            args.speech_cpp_binary, args.speech_cpp_model,
            num_threads=config.asr.cpu_threads, keywords=keywords,
        ))
    if any((args.speech_cpp_binary, args.speech_cpp_model, args.speech_cpp_keywords)):
        raise ValueError("--speech-cpp-* には asr.engine: speech-cpp が必要です")
    return build_asr_engine(config)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audio", type=Path, help="授業の動画または音声")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--config-override", type=Path, help="既定の設定に重ねる設定")
    parser.add_argument(
        "--segment-config", type=Path, help="Segment の区切りだけこの設定に揃える"
    )
    parser.add_argument("--ref", type=Path, help="Segment 番号→正解文の JSON")
    parser.add_argument("--out", type=Path, help="書き起こしの保存先 JSON")
    parser.add_argument("--candidate-out", type=Path, help="下書き用の書き起こしと音声区間の JSON")
    parser.add_argument("--terms", type=Path, help="再現数を数える用語（1行1語）")
    parser.add_argument("--hotwords", default=None, help="既定は config.yaml の値。none で無効")
    parser.add_argument("--hotwords-score", type=float, default=None)
    parser.add_argument("--blank-penalty", type=float, default=None)
    parser.add_argument("--refill-gap", type=float, default=None, help="0 で再デコードによる補完を無効")
    parser.add_argument("--speech-cpp-binary", type=Path)
    parser.add_argument("--speech-cpp-model", type=Path)
    parser.add_argument("--speech-cpp-keywords", type=Path, help="1 行 1 語の UTF-8 語彙ファイル")
    args = parser.parse_args()

    try:
        outputs = [outside_repo(path) for path in (args.out, args.candidate_out) if path]
        inputs = [path.resolve() for path in (
            args.audio, args.ref, args.terms, args.config, args.config_override, args.segment_config,
            args.speech_cpp_binary, args.speech_cpp_model, args.speech_cpp_keywords,
        ) if path]
        if len(set(outputs)) != len(outputs) or set(outputs).intersection(inputs):
            raise ValueError("入力と各出力には別々のファイルを指定してください")
        if args.config_override is not None and not args.config_override.is_file():
            raise FileNotFoundError(args.config_override)
        config = load_config(args.config, override=args.config_override)
        segmentation = load_config(args.segment_config) if args.segment_config else config
        pcm, digest = load_candidate_audio(args.audio) if args.candidate_out else (load_audio(args.audio), "")
        speech = segments(segmentation, pcm)
        ref = TypeAdapter(dict[str, str]).validate_json(args.ref.read_bytes()) if args.ref else None
        if ref is not None and set(ref) != {str(i) for i in range(len(speech))}:
            print("参照と Segment 数が合わない（vad / turn の設定が参照作成時と違う）", file=sys.stderr)
            return 1
        with ExitStack() as stack:
            engine = build_evaluation_engine(config, args, stack)
            results, elapsed = evaluate(engine, speech)

        speech_s = sum(segment.pcm.size for segment in speech) / SAMPLE_RATE
        print(f"segments={len(speech)} speech={speech_s:.1f}s asr={elapsed:.1f}s")
        hyp = {index: segment.text for index, segment in results.items()}
        if args.out:
            args.out.write_text(json.dumps(hyp, ensure_ascii=False, indent=1), encoding="utf-8")
        if args.candidate_out:
            run = CandidateRun(audio_sha256=digest, segments=results)
            args.candidate_out.write_text(run.model_dump_json(indent=2), encoding="utf-8")
        if ref is not None:
            terms = args.terms.read_text(encoding="utf-8").split() if args.terms else []
            print(score(ref, hyp, terms))
    except (OSError, RuntimeError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
