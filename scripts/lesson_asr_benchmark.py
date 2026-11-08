"""Measure the ASR candidates against the same lesson and regression corpus."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import locale
import logging
import re
import socket
import statistics
import subprocess
import sys
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

import yaml

from scripts.lesson_asr_eval import (
    ROOT, SAMPLE_RATE, CandidateRun, build_evaluation_engine, load_candidate_audio, outside_repo,
)
from server.config import load_config

GEOGRAPHY = ROOT / "server/asr/hotwords/geography.txt"
LESSON = ROOT / "TestData/testdata.mp4"
CONFIG = ROOT / "config.yaml"
PROFILES = ("reazon", "haas-fp32", "haas-int4", "haas-int8", "speech-q4_k", "speech-q8_0")


def write_profiles(out: Path, haas: Path, speech: Path) -> list[tuple[str, str]]:
    terms = GEOGRAPHY.read_text(encoding="utf-8").splitlines()
    tokens = {
        line.rsplit(maxsplit=1)[0]
        for line in (haas / "haas-ja-v1.0-onnx/tokens.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    sherpa_words = out / "haas-hotwords.txt"
    supported = [term for term in terms if set(term) <= tokens]
    if not supported:
        raise ValueError("Haas の辞書に登録できる教科用語がありません")
    sherpa_words.write_text("".join(" ".join(term) + "\n" for term in supported), encoding="utf-8")
    variants = [
        ("reazon", {"engine": "sherpa"}, {
            "sherpa": {
                "decoding_method": "greedy_search", "hotwords_file": None,
                "blank_penalty": 0, "refill_gap_s": 0,
            },
        }, {}),
        ("haas-fp32", {
            "engine": "haas-sherpa",
            "haas_sherpa": {"model": str(haas / "haas-ja-v1.0-onnx")},
        }, {}, {
            "haas_sherpa": {
                "model": str(haas / "haas-ja-v1.0-onnx"),
                "decoding_method": "modified_beam_search",
                "hotwords_file": str(sherpa_words),
            },
        }),
    ]
    for quant in ("int4", "int8"):
        model = {"model": str(haas / "haas-ja-onecompression"), "quantization": quant}
        variants.append((f"haas-{quant}", {"engine": "haas-onnx"}, {
            "haas_onnx": {**model, "cpu_threads": 4},
        }, {
            "haas_onnx": {**model, "cpu_threads": 6},
        }))
    for quant in ("Q4_K", "Q8_0"):
        variants.append((f"speech-{quant.lower()}", {
            "engine": "speech-cpp",
        }, {}, {}))

    profiles = []
    for name, common, plain, tuned in variants:
        for stage, changes in (("plain", plain), ("tuned", tuned)):
            profile = {"asr": {"cpu_threads": 4, **common, **changes}}
            if common["engine"] == "speech-cpp":
                quant = name.removeprefix("speech-").upper()
                profile["speech_cpp"] = {
                    "binary": str(speech / "speech/speech.exe"),
                    "model": str(speech / f"Haas-616M-ja-v1.0-{quant}.gguf"),
                    "keywords": str(GEOGRAPHY) if stage == "tuned" else None,
                }
            (out / f"{name}-{stage}.yaml").write_text(
                yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8",
            )
            profiles.append((name, stage))
    return profiles


def speech_options(profile: dict) -> list[str]:
    return [
        option
        for key, path in profile.get("speech_cpp", {}).items() if path
        for option in (f"--speech-cpp-{key}", path)
    ]


def accuracy(out: Path, reference: Path, haas: Path, speech: Path) -> None:
    out.mkdir(parents=True, exist_ok=False)
    reference_sha256 = hashlib.sha256(reference.read_bytes()).hexdigest()
    video_sha256 = hashlib.sha256(LESSON.read_bytes()).hexdigest()
    (out / "inputs.json").write_text(json.dumps({
        "reference_sha256": reference_sha256,
        "video_sha256": video_sha256,
    }, indent=2), encoding="utf-8")
    failed = []
    for name, stage in write_profiles(out, haas, speech):
        label = f"{name}-{stage}"
        profile_path = out / f"{label}.yaml"
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        command = [
            sys.executable, "-B", "-m", "scripts.lesson_asr_eval", str(LESSON),
            "--config-override", str(profile_path), "--segment-config", str(CONFIG),
            "--ref", str(reference), "--terms", str(GEOGRAPHY),
            "--candidate-out", str(out / f"{label}.candidate.json"),
            *speech_options(profile),
        ]
        with (out / f"{label}.log").open("x", encoding="utf-8") as log:
            result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        print(f"{label}: exit {result.returncode}", flush=True)
        if result.returncode:
            failed.append(label)
    if hashlib.sha256(reference.read_bytes()).hexdigest() != reference_sha256:
        raise ValueError("計測中に正解文が変更されました")
    if failed:
        raise RuntimeError(f"認識精度の計測に失敗しました: {', '.join(failed)}")


def load_profile(path: Path, stack: ExitStack):
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    options = profile.get("speech_cpp", {})
    config = load_config(CONFIG, override=path)
    engine = build_evaluation_engine(config, argparse.Namespace(
        hotwords=None, hotwords_score=None, blank_penalty=None, refill_gap=None,
        speech_cpp_binary=Path(options["binary"]) if options.get("binary") else None,
        speech_cpp_model=Path(options["model"]) if options.get("model") else None,
        speech_cpp_keywords=Path(options["keywords"]) if options.get("keywords") else None,
    ), stack)
    return config, engine


def serve(profile: Path, port: int) -> None:
    import uvicorn

    from server.main import create_app
    from server.publication import Publication

    logging.basicConfig(level=logging.INFO)
    with ExitStack() as stack:
        config, engine = load_profile(profile, stack)
        config.server.http_port = port
        config.mt.cache_size = 0
        config.recording.default_on = False
        publication = Publication(config.server)
        app = create_app(config, publication, asr_engine=engine)
        uvicorn.run(app, host="127.0.0.1", port=port, proxy_headers=False, access_log=False)


def read_server_log(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


def replay(profile: Path, out: Path, seconds: float | None) -> bool:
    import psutil

    from scripts import replay_client
    from scripts.text_metrics import normalize_ja

    profile_digest = hashlib.sha256(profile.read_bytes()).hexdigest()
    pcm, digest = load_candidate_audio(LESSON)
    if seconds is not None:
        pcm = pcm[:round(seconds * SAMPLE_RATE)]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    host = {
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(),
        "cpu_percent_before": psutil.cpu_percent(interval=1),
        "available_memory_bytes_before": psutil.virtual_memory().available,
    }
    with out.with_suffix(".log").open("x", encoding="utf-8") as log:
        process = subprocess.Popen([
            sys.executable, "-B", "-m", "scripts.lesson_asr_benchmark",
            "serve", str(profile), str(port),
        ], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        try:
            replay_client.wait_ready(port, process)
            codes = replay_client.fetch_codes(port)
            print(f"{profile.stem}: replaying {pcm.size / SAMPLE_RATE:.2f}s", flush=True)
            results = asyncio.run(replay_client._drive(
                port, codes, pcm, 2, 20, process, ["en", "zh"],
            ))
        finally:
            replay_client._terminate_tree(process)

    delays = [
        delay for (seq, lang), delay in results.captions.items()
        if seq in results.published_seqs and lang in ("en", "zh")
    ]
    stats = results.stats_samples[-1] if results.stats_samples else {}
    expected_audio_bytes = pcm.nbytes
    dropped = asdict(results.dropped_audio())
    expected_captions = len(results.published_seqs) * 2
    delivery = dict(Counter(lang for _, lang in results.captions))
    server_log = read_server_log(out.with_suffix(".log"))
    server_errors = len(re.findall(r"^ERROR[: ]", server_log, flags=re.MULTILINE))
    evidence = {
        "video_sha256": digest,
        "profile_sha256": profile_digest,
        "host": host,
        "target_seconds": pcm.size / SAMPLE_RATE,
        "measurement_seconds": results.measurement_seconds,
        "audio_bytes_expected": expected_audio_bytes,
        "audio_bytes_sent": results.audio_bytes_sent,
        "students": results.measurement_langs,
        "captions": delivery,
        "published_turns": len(results.published_seqs),
        "asr_calls": stats.get("asr_calls"),
        "mt_calls": stats.get("mt_calls"),
        "cache_hits": stats.get("mt_cache_hits"),
        "notices": results.notices,
        "dropped": dropped,
        "disconnects": results.disconnects,
        "crashes": results.crashes,
        "errors": results.errors,
        "server_errors": server_errors,
        "median_delay_ms": statistics.median(delays) if delays else None,
        "max_delay_ms": max(delays) if delays else None,
        "peak_rss_bytes": round(max(results.rss_mb) * 1e6) if results.rss_mb else None,
        "rss_interval_s": replay_client.RSS_INTERVAL_S,
        "delays_ms": delays,
        "resource_samples": results.resource_samples,
        "stats_samples": results.stats_samples,
    }
    if seconds is None:
        offline = CandidateRun.model_validate_json(profile.with_suffix(".candidate.json").read_bytes())
        live_text = "".join(text for _, text in sorted(results.finals.items()))
        offline_text = "".join(offline.segments[str(i)].text for i in range(len(offline.segments)))
        evidence["offline_text_matches"] = normalize_ja(live_text) == normalize_ja(offline_text)
    valid = (
        results.audio_bytes_sent == expected_audio_bytes
        and results.measurement_langs == {"en": 1, "zh": 1}
        and bool(delays) and len(delays) == expected_captions == len(results.captions)
        and stats.get("mt_calls") == expected_captions
        and bool(stats.get("asr_calls")) and stats.get("mt_cache_hits") == 0
        and (seconds is not None or stats["asr_calls"] == 50)
        and bool(results.resource_samples)
        and results.resource_samples[-1]["at_s"] >= results.measurement_seconds
        and not any(dropped.values())
        and not (results.errors or results.crashes or results.disconnects or server_errors)
        and not (set(results.notices) - {"mic_silent"})
        and hashlib.sha256(profile.read_bytes()).hexdigest() == profile_digest
    )
    evidence["valid"] = valid
    with out.open("x", encoding="utf-8") as stream:
        json.dump(evidence, stream, ensure_ascii=False, indent=2)
    print(f"{profile.stem}: valid={valid}, ASR={evidence['asr_calls']}, "
          f"MT={evidence['mt_calls']}, max={evidence['max_delay_ms']} ms", flush=True)
    return valid


def regression(profile: Path, out: Path) -> None:
    from scripts.bench import load_ext_clips
    from scripts.replay_client import read_wav_16k_mono
    from scripts.text_metrics import EditCounts, cer_counts, normalize_ja

    known = {"base-01": "今日は植物", "base-02": "光合成", "base-08": "原子の数"}
    clips = load_ext_clips()
    browser_dir = ROOT / "artifacts/asr-candidates"
    dataset = json.loads((browser_dir / "dataset.json").read_text(encoding="utf-8"))
    browser_clips = [clip for clip in dataset if clip["group"] == "browser"]
    counts = EditCounts()
    noise_insertions = {}
    known_correct = {}
    browser_results = {}
    with ExitStack() as stack:
        _, engine = load_profile(profile, stack)
        engine.warmup()
        for clip in clips:
            clip_id = clip["id"]
            text = engine.transcribe(clip["pcm"], SAMPLE_RATE).text
            edits = cer_counts(clip["text"], text)
            if edits.ref_len:
                counts += edits
            else:
                noise_insertions[clip_id] = edits.insertions
            if clip_id in known:
                known_correct[clip_id] = known[clip_id] in normalize_ja(text)
        for clip in browser_clips:
            path = browser_dir / "audio" / f"{clip['id']}.wav"
            if hashlib.sha256(path.read_bytes()).hexdigest() != clip["sha256"]:
                raise ValueError(f"{clip['id']} の音源が回帰コーパスと一致しません")
            text = engine.transcribe(read_wav_16k_mono(path), SAMPLE_RATE).text
            browser_results[clip["id"]] = {
                "counts": asdict(cer_counts(clip["reference"], text)),
                "phrase_preserved": known["base-01"] in normalize_ja(text),
            }
    if len(clips) != 35 or len(noise_insertions) != 1 or set(known_correct) != set(known) or len(browser_results) != 3:
        raise ValueError("回帰コーパスの構成が想定と異なります")
    with out.open("x", encoding="utf-8") as stream:
        json.dump({
            "speech_clips": len(clips) - len(noise_insertions),
            "counts": asdict(counts),
            "noise_insertions": noise_insertions,
            "known_correct": known_correct,
            "browser_inputs": browser_results,
        }, stream, indent=2)
    print(f"{profile.stem}: regression corpus complete", flush=True)


def punctuation(out: Path) -> None:
    from scripts.bench import _PUNCT_RE, build_segmenter, phase_mt_punct, segment_clip
    from server.turn import TurnAssembler, build_boundary_classifier
    from server.turn.assembler import TurnPart

    config = load_config(CONFIG)
    run = CandidateRun.model_validate_json((out.parent / "haas-int8-tuned.candidate.json").read_bytes())
    pcm, digest = load_candidate_audio(LESSON)
    if run.audio_sha256 != digest:
        raise ValueError("句読点の確認に使う候補出力が入力動画と一致しません")
    segmenter, frame_vad = build_segmenter(config, config.turn)
    events = segment_clip(segmenter, pcm, SAMPLE_RATE * frame_vad.frame_ms // 1000)
    turn_results = {}
    for keep_marks in (True, False):
        assembler = TurnAssembler(
            build_boundary_classifier(config.turn.classifier), strategy=config.turn.strategy,
            max_turn_s=config.turn.morph.max_turn_s, max_segments=config.turn.morph.max_segments,
        )
        turns = []
        index = 0
        for event in events:
            segment = event["seg"]
            if segment is None:
                turn = assembler.on_boundary()
            else:
                hypothesis = run.segments[str(index)]
                start = round(segment.t_start * SAMPLE_RATE)
                if (start, start + segment.pcm.size) != (hypothesis.start_sample, hypothesis.end_sample):
                    raise ValueError(f"句読点の確認で Segment {index} の区間が一致しません")
                text = hypothesis.text if keep_marks else _PUNCT_RE.sub("", hypothesis.text)
                turn = assembler.add_segment(TurnPart(
                    text=text, t_start=segment.t_start, t_end=segment.t_end, asr_ms=0,
                    closed_at=event["closed_at_s"], audio_s=segment.pcm.size / SAMPLE_RATE,
                ))
                index += 1
            if turn is not None:
                turns.append(turn)
        if index != len(run.segments):
            raise ValueError("句読点の確認で Segment 数が一致しません")
        tail = assembler.flush()
        if tail is not None:
            turns.append(tail)
        turn_results["with" if keep_marks else "without"] = {
            "count": len(turns),
            "reasons": dict(Counter(turn.reason for turn in turns)),
            "endpoints": [round(turn.t_end, 3) for turn in turns],
        }
    result = phase_mt_punct(config.models.resolved_dir, config)
    result["lesson_turns"] = turn_results
    with out.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)


def package_sizes(haas: Path, speech: Path, out: Path) -> None:
    from server.asr.haas_sherpa_engine import MODEL_FILES

    legal = ["LICENSE", "NOTICE"]
    fp32 = haas / "haas-ja-v1.0-onnx"
    kizuna = haas / "haas-ja-onecompression"
    manifest = json.loads((kizuna / "manifest.json").read_text(encoding="utf-8"))
    files = {
        "reazon": [],
        "haas-fp32": [fp32 / name for name in (*MODEL_FILES, *legal)],
    }
    for quant, key in (("int4", "int4-onecomp"), ("int8", "int8")):
        encoder = manifest["formats"][key]["encoder"]
        files[f"haas-{quant}"] = [
            kizuna / name for name in (
                "manifest.json", encoder, f"{encoder}.data", *manifest["common"].values(), *legal,
            )
        ]
    for quant in ("Q4_K", "Q8_0"):
        files[f"speech-{quant.lower()}"] = [
            speech / "speech/speech.exe", speech / f"Haas-616M-ja-v1.0-{quant}.gguf",
        ]
    with out.open("x", encoding="utf-8") as stream:
        sizes = {}
        for name, paths in files.items():
            entries = {path.name: path.stat().st_size for path in paths}
            sizes[name] = {"files": entries, "payload_bytes": sum(entries.values())}
        json.dump(sizes, stream, indent=2)


def summarize(directory: Path, reference: Path) -> dict:
    from scripts.asr_adoption import MAX_DELAY_LIMIT_MS, PEAK_RSS_LIMIT_BYTES, evaluate_json
    from scripts.replay_client import _timing_coverage
    from scripts.text_metrics import EditCounts, cer_counts

    inputs = json.loads((directory / "inputs.json").read_text(encoding="utf-8"))
    reference_bytes = reference.read_bytes()
    if hashlib.sha256(reference_bytes).hexdigest() != inputs["reference_sha256"]:
        raise ValueError("正解文が認識精度の計測時と一致しません")
    if hashlib.sha256(LESSON.read_bytes()).hexdigest() != inputs["video_sha256"]:
        raise ValueError("動画が認識精度の計測時と一致しません")
    refs = json.loads(reference_bytes)
    if set(refs) != {str(i) for i in range(50)} or not all(isinstance(ref, str) for ref in refs.values()):
        raise ValueError("正解文の Segment が想定と一致しません")
    terms = GEOGRAPHY.read_text(encoding="utf-8").split()
    sizes = json.loads((directory / "package-sizes.json").read_text(encoding="utf-8"))
    results = {}
    expected_ranges = None
    for name in PROFILES:
        accuracy_scores = {}
        for stage in ("plain", "tuned"):
            path = directory / f"{name}-{stage}.candidate.json"
            run = CandidateRun.model_validate_json(path.read_bytes())
            ranges = {i: (seg.start_sample, seg.end_sample) for i, seg in run.segments.items()}
            if expected_ranges is None:
                expected_ranges = ranges
            if run.audio_sha256 != inputs["video_sha256"] or set(ranges) != set(refs) or ranges != expected_ranges:
                raise ValueError(f"{name}-{stage} の入力動画または区間が一致しません")
            counts = sum((cer_counts(ref, run.segments[i].text) for i, ref in refs.items()), EditCounts())
            accuracy_scores[stage] = {
                "cer": counts.error_rate,
                **asdict(counts),
                "nonempty_segments": sum(bool(seg.text.strip()) for seg in run.segments.values()),
                "terms_recovered": sum(
                    min(ref.count(term), run.segments[i].text.count(term))
                    for i, ref in refs.items() for term in terms
                ),
                "terms_total": sum(ref.count(term) for ref in refs.values() for term in terms),
            }

        regression_result = json.loads((directory / f"{name}.regression.json").read_text(encoding="utf-8"))
        profile_digest = hashlib.sha256((directory / f"{name}-tuned.yaml").read_bytes()).hexdigest()
        runs = []
        for path in sorted(directory.glob(f"{name}.replay.*.json")):
            run = json.loads(path.read_text(encoding="utf-8"))
            coverage = _timing_coverage(run["resource_samples"], run["measurement_seconds"])
            gap = coverage["max_gap_s"]
            filtered = read_server_log(path.with_suffix(".log")).count("発話を破棄（幻覚フィルタ:")
            failed_checks = []
            if not run["valid"]:
                failed_checks.append("delivery")
            if gap is None or gap > 3 * run["rss_interval_s"]:
                failed_checks.append("rss sampling")
            if abs(run["target_seconds"] - run["measurement_seconds"]) > 1:
                failed_checks.append("playback rate")
            if filtered:
                failed_checks.append("discarded ASR segments")
            if run.get("offline_text_matches") is False:
                failed_checks.append("live/offline text mismatch")
            run.update(resource_coverage=coverage, filtered_segments=filtered, failed_checks=failed_checks)
            runs.append(run)
        if any(
            run["video_sha256"] != inputs["video_sha256"] or run["profile_sha256"] != profile_digest
            or run["target_seconds"] < 615
            for run in runs
        ):
            raise ValueError(f"{name} の実時間計測の入力が一致しません")
        sampled_runs = [
            run for run in runs
            if {"rss sampling", "playback rate"}.isdisjoint(run["failed_checks"])
            and run["delays_ms"] and run["peak_rss_bytes"] is not None
        ]
        if not runs:
            status = "unmeasured"
        elif len(sampled_runs) != len(runs):
            status = "inconclusive"
        elif any(run["failed_checks"] for run in runs):
            status = "delivery_failed"
        else:
            status = "measured"
        results[name] = {
            "accuracy": accuracy_scores,
            "regression": regression_result,
            "package_size": sizes[name],
            "runtime_status": status,
            "replays": [
                {key: value for key, value in run.items()
                 if key not in ("resource_samples", "stats_samples", "delays_ms")}
                for run in runs
            ],
            "runtime": {
                "median_delay_ms": max(run["median_delay_ms"] for run in sampled_runs),
                "max_delay_ms": max(run["max_delay_ms"] for run in sampled_runs),
                "peak_rss_bytes": max(run["peak_rss_bytes"] for run in sampled_runs),
            } if sampled_runs else None,
        }
    candidates = {}
    excluded = {}
    for name, row in results.items():
        if name == "reazon":
            continue
        runtime = row["runtime"]
        status = row["runtime_status"]
        exceeds_limit = runtime is not None and (
            runtime["max_delay_ms"] > MAX_DELAY_LIMIT_MS
            or runtime["peak_rss_bytes"] > PEAK_RSS_LIMIT_BYTES
        )
        if status == "measured" or (status == "delivery_failed" and exceeds_limit):
            candidates[name] = {
                "cer": row["accuracy"]["tuned"]["cer"],
                "package_growth_bytes": row["package_size"]["payload_bytes"],
                **runtime,
            }
        else:
            excluded[name] = status
    measurements = {
        "baseline_cer": results["reazon"]["accuracy"]["tuned"]["cer"],
        "candidates": candidates,
    }
    verdict = asdict(evaluate_json(json.dumps(measurements))) if all(
        row["runtime_status"] in ("measured", "delivery_failed") for row in results.values()
    ) else None
    marks = json.loads((directory / "punctuation.json").read_text(encoding="utf-8"))
    del marks["items"]
    for row in marks["lesson_turns"].values():
        del row["endpoints"]
    return {
        **measurements,
        "package_growth_is_estimate": True,
        "inputs": inputs,
        "profiles": results,
        "punctuation": marks,
        "excluded_before_decision": excluded,
        "decision": verdict,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    score = commands.add_parser("accuracy")
    score.add_argument("--out", type=Path, required=True)
    score.add_argument("--ref", type=Path, required=True)
    score.add_argument("--haas-models", type=Path, required=True)
    score.add_argument("--speech-cpp-dir", type=Path, required=True)
    live = commands.add_parser("replay")
    live.add_argument("profile", type=Path)
    live.add_argument("--out", type=Path, required=True)
    live.add_argument("--seconds", type=float)
    corpus = commands.add_parser("regression")
    corpus.add_argument("profile", type=Path)
    corpus.add_argument("--out", type=Path, required=True)
    marks = commands.add_parser("punctuation")
    marks.add_argument("--out", type=Path, required=True)
    sizes = commands.add_parser("package-sizes")
    sizes.add_argument("--out", type=Path, required=True)
    sizes.add_argument("--haas-models", type=Path, required=True)
    sizes.add_argument("--speech-cpp-dir", type=Path, required=True)
    report = commands.add_parser("summarize")
    report.add_argument("directory", type=Path)
    report.add_argument("--ref", type=Path, required=True)
    report.add_argument("--out", type=Path)
    child = commands.add_parser("serve")
    child.add_argument("profile", type=Path)
    child.add_argument("port", type=int)
    args = parser.parse_args()
    if args.command == "accuracy":
        accuracy(outside_repo(args.out), args.ref.resolve(), args.haas_models.resolve(),
                 args.speech_cpp_dir.resolve())
    elif args.command == "replay":
        if not replay(args.profile.resolve(), outside_repo(args.out), args.seconds):
            raise SystemExit(1)
    elif args.command == "regression":
        regression(args.profile.resolve(), outside_repo(args.out))
    elif args.command == "punctuation":
        punctuation(outside_repo(args.out))
    elif args.command == "package-sizes":
        package_sizes(args.haas_models, args.speech_cpp_dir, outside_repo(args.out))
    elif args.command == "summarize":
        report_data = summarize(args.directory, args.ref)
        if args.out:
            with args.out.open("x", encoding="utf-8") as stream:
                json.dump(report_data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        else:
            print(json.dumps(report_data, ensure_ascii=True, indent=2))
    else:
        serve(args.profile.resolve(), args.port)


if __name__ == "__main__":
    main()
