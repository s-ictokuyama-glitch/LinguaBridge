"""CER/WER 算出（#24 ベースライン計測）のユニットテスト。

数えているのは「編集距離の内訳」と「正規化がどこまで畳むか」の2点。
正規化を緩めすぎると精度差が消え、厳しすぎると表記仕様の差が精度差に化けるので、
畳む/畳まないの境界をテストで固定する。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import wave

import pytest

import scripts.quality_evidence as quality_evidence
from scripts.quality_evidence import main as quality_evidence_main
from scripts.quality_evidence import validate_quality_package
from scripts.quality_evidence import validate_quality_tsv
from scripts.text_metrics import (
    EditCounts,
    cer_counts,
    corpus_rate,
    edit_counts,
    normalize_ja,
    resolve_tokenizer,
    tokenize_charclass,
    wer_counts,
)


def _run_quality_with_a_stale_qa_helper_pyc(
    tmp_path: Path,
) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "stale-pyc-repository"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "__init__.py").write_text("", encoding="utf-8")
    shutil.copyfile(Path(quality_evidence.__file__), scripts / "quality_evidence.py")
    helper = scripts / "qa_evidence_manifest.py"
    good = Path(quality_evidence.qa_evidence_manifest.__file__).read_bytes()
    needle = b"from typing import Any, Callable, Iterable, cast"
    poison = b'raise RuntimeError("STALE PYC EXECUTED")'
    assert needle in good and len(poison) <= len(needle)
    bad = good.replace(needle, poison + b" " * (len(needle) - len(poison)), 1)
    assert len(bad) == len(good)
    fixed_ns = 1_700_000_000_000_000_000
    helper.write_bytes(bad)
    os.utime(helper, ns=(fixed_ns, fixed_ns))
    cache = helper.parent / "__pycache__" / (
        f"{helper.stem}.{sys.implementation.cache_tag}.pyc"
    )
    cache.parent.mkdir()
    py_compile.compile(str(helper), cfile=str(cache), doraise=True)
    helper.write_bytes(good)
    os.utime(helper, ns=(fixed_ns, fixed_ns))
    environment = os.environ.copy()
    environment.pop("PYTHONPYCACHEPREFIX", None)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    return subprocess.run(
        [sys.executable, str(scripts / "quality_evidence.py"), "--help"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_quality_standalone_import_ignores_a_same_size_stale_qa_helper_pyc(
    tmp_path: Path,
) -> None:
    completed = _run_quality_with_a_stale_qa_helper_pyc(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert "STALE PYC EXECUTED" not in completed.stderr


def test_quality_import_bootstrap_uses_empty_private_cache_and_pre_post_binding() -> None:
    cache = Path(quality_evidence._IMPORT_PYCACHE.name)
    assert quality_evidence._IMPORT_PYCACHE_WAS_EMPTY is True
    assert cache.is_dir()
    expected_hashes = {
        path.relative_to(quality_evidence._IMPORT_ROOT).as_posix(): state[0]
        for path, state in quality_evidence._EARLY_IMPORTED_VALIDATOR_STATE.items()
    }
    expected_revisions = {
        path.relative_to(quality_evidence._IMPORT_ROOT).as_posix(): state[1]
        for path, state in quality_evidence._EARLY_IMPORTED_VALIDATOR_STATE.items()
    }
    assert dict(quality_evidence._IMPORTED_VALIDATOR_SOURCE_SNAPSHOT) == expected_hashes
    assert dict(quality_evidence._IMPORTED_VALIDATOR_SOURCE_SNAPSHOT.revisions) == (
        expected_revisions
    )
    source = Path(quality_evidence.__file__).read_text(encoding="utf-8")
    assert source.index("sys.pycache_prefix = _IMPORT_PYCACHE.name") < source.index(
        "from scripts import qa_evidence_manifest"
    )


# ---- 正規化 ----


def test_normalize_drops_punctuation_and_space():
    # 句読点を出せないエンジン（#21 の ReazonSpeech K2）を表記仕様で減点しないため
    assert normalize_ja("今日は、晴れです。") == "今日は晴れです"
    assert normalize_ja("あ い\u3000う") == "あいう"


def test_normalize_keeps_punctuation_when_asked():
    assert normalize_ja("今日は、晴れです。", drop_marks=False) == "今日は、晴れです。"


def test_normalize_folds_halfwidth_kana_and_fullwidth_digits():
    assert normalize_ja("ｱｲｳ") == "アイウ"
    assert normalize_ja("４２ページ") == "42ページ"


def test_normalize_does_not_fold_homophone_errors():
    # 「光合成→構合性」は精度の誤りであって表記ゆれではない。畳んではいけない
    assert normalize_ja("光合成") != normalize_ja("構合性")


# ---- 編集距離 ----


def test_edit_counts_identical_is_zero():
    c = edit_counts("あいう", "あいう")
    assert c.distance == 0 and c.error_rate == 0.0


def test_edit_counts_splits_substitution_deletion_insertion():
    assert edit_counts("あいう", "あXう") == EditCounts(substitutions=1, ref_len=3)
    assert edit_counts("あいう", "あう") == EditCounts(deletions=1, ref_len=3)
    assert edit_counts("あいう", "あいうえ") == EditCounts(insertions=1, ref_len=3)


def test_error_rate_is_none_when_reference_is_empty():
    # noise-only クリップ（正解テキストが空）は誤り率が定義できない。
    # 幻覚の量は挿入文字数で見る
    c = edit_counts("", "あーうー")
    assert c.error_rate is None
    assert c.insertions == 4


# ---- CER ----


def test_cer_counts_homophone_error():
    c = cer_counts("今日は植物の光合成について勉強します。", "今日は植物の構合性について勉強します")
    assert (c.substitutions, c.deletions, c.insertions) == (2, 0, 0)
    assert c.ref_len == 18  # 句読点を除いた文字数
    assert c.error_rate == pytest.approx(2 / 18)


def test_cer_ignores_trailing_punctuation_difference():
    assert cer_counts("はい、それでは。", "はい それでは").distance == 0


def test_cer_counts_punctuation_when_marks_kept():
    assert cer_counts("はい、それでは。", "はい それでは", drop_marks=False).distance > 0


# ---- WER ----


def test_charclass_tokenizer_splits_on_script_boundaries():
    assert tokenize_charclass("光合成について") == ["光合成", "について"]
    assert tokenize_charclass("42ページ") == ["42", "ページ"]


def test_wer_counts_uses_tokens_not_characters():
    # 3文字の誤りでも1トークンの置換
    c = wer_counts("光合成について", "構合性について")
    assert c == EditCounts(substitutions=1, ref_len=2)


def test_resolve_tokenizer_falls_back_when_sudachi_missing():
    # 形態素解析器の採否は #27 の決定。未導入環境でも WER が出せること
    name, tokenize = resolve_tokenizer("sudachi")
    assert name in ("sudachi", "charclass")
    assert tokenize("光合成")


def test_resolve_tokenizer_rejects_unknown_name():
    with pytest.raises(ValueError):
        resolve_tokenizer("mecab")


# ---- コーパス集計 ----


def test_corpus_rate_weights_by_reference_length_not_by_clip():
    # 長いクリップ1件（誤り0/100）と短いクリップ1件（誤り1/2）。
    # クリップ平均なら 0.25 だが、総和なら 1/102
    counts = [EditCounts(ref_len=100), EditCounts(substitutions=1, ref_len=2)]
    assert corpus_rate(counts) == pytest.approx(1 / 102)


def test_corpus_rate_is_none_when_all_references_empty():
    assert corpus_rate([EditCounts(insertions=3, ref_len=0)]) is None


# ---- QUAL-01 field evidence ----


QUALITY_HEADER = [
    "No",
    "領域",
    "音響条件",
    "正解日本語",
    "ASR日本語",
    "英語訳",
    "英語評点1-5",
    "中国語訳",
    "中国語評点1-5",
    "所見",
]


def _quality_rows() -> list[list[str]]:
    domains = ["理科", "数学", "行事"]
    acoustics = ["後方", "雑音", "チャイム"]
    return [
        [
            str(number),
            domains[(number - 1) % len(domains)],
            acoustics[(number - 1) % len(acoustics)],
            f"授業文 {number}",
            f"認識文 {number}",
            f"English {number}",
            "4",
            f"中文 {number}",
            "3" if number <= 15 else "4",
            "",  # 所見は問題がない行では任意。
        ]
        for number in range(1, 31)
    ]


def _write_quality_tsv(
    tmp_path: Path,
    rows: list[list[str]],
    *,
    header: list[str] | None = None,
) -> Path:
    path = tmp_path / "2026-09-08-quality-30.tsv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(QUALITY_HEADER if header is None else header)
        writer.writerows(rows)
    return path


def test_quality_evidence_accepts_exact_threshold_and_optional_notes(
    tmp_path: Path,
) -> None:
    result = validate_quality_tsv(_write_quality_tsv(tmp_path, _quality_rows()))

    assert result["passed"] is True
    assert result["checks"] == {
        "header_exact": True,
        "exactly_30_rows": True,
        "exactly_10_columns": True,
        "numbers_1_through_30_in_order": True,
        "thirty_distinct_normalized_references": True,
        "required_fields_complete": True,
        "thirty_valid_english_ratings": True,
        "thirty_valid_chinese_ratings": True,
        "required_domains_covered": True,
        "required_acoustics_covered": True,
        "chinese_average_at_least_3_5": True,
    }
    assert result["metrics"] == {
        "rows": 30,
        "required_rows": 30,
        "english_rating_count": 30,
        "english_average": 4.0,
        "chinese_rating_count": 30,
        "chinese_average": 3.5,
        "ordered_number_count": 30,
        "distinct_reference_count": 30,
        "domain_coverage_count": 3,
        "acoustic_coverage_count": 3,
    }
    assert result["scope"]["directory_hash_completeness_checked"] is False


@pytest.mark.parametrize(
    ("case", "failed_check"),
    [
        ("29 rows", "exactly_30_rows"),
        ("31 rows", "exactly_30_rows"),
        ("wrong header", "header_exact"),
        ("short row", "exactly_10_columns"),
        ("extra column", "exactly_10_columns"),
        ("numbers out of order", "numbers_1_through_30_in_order"),
        (
            "whitespace-normalized duplicate",
            "thirty_distinct_normalized_references",
        ),
        ("missing ASR", "required_fields_complete"),
        ("missing English translation", "required_fields_complete"),
        ("missing Chinese translation", "required_fields_complete"),
        ("English rating zero", "thirty_valid_english_ratings"),
        ("Chinese rating six", "thirty_valid_chinese_ratings"),
        ("fractional rating", "thirty_valid_chinese_ratings"),
        ("missing domain", "required_domains_covered"),
        ("negated domains", "required_domains_covered"),
        ("missing acoustic", "required_acoustics_covered"),
        ("negated acoustics", "required_acoustics_covered"),
        ("Chinese average below threshold", "chinese_average_at_least_3_5"),
    ],
)
def test_quality_evidence_rejects_false_pass_inputs(
    tmp_path: Path, case: str, failed_check: str
) -> None:
    rows = _quality_rows()
    header = QUALITY_HEADER.copy()

    if case == "29 rows":
        rows.pop()
    elif case == "31 rows":
        rows.append(["31", *rows[-1][1:]])
    elif case == "wrong header":
        header[3] = "原文"
    elif case == "short row":
        rows[0].pop()
    elif case == "extra column":
        rows[0].append("余分")
    elif case == "numbers out of order":
        rows[0], rows[1] = rows[1], rows[0]
    elif case == "whitespace-normalized duplicate":
        rows[1][3] = "  授業文\u30001  "
    elif case == "missing ASR":
        rows[0][4] = ""
    elif case == "missing English translation":
        rows[0][5] = ""
    elif case == "missing Chinese translation":
        rows[0][7] = ""
    elif case == "English rating zero":
        rows[0][6] = "0"
    elif case == "Chinese rating six":
        rows[0][8] = "6"
    elif case == "fractional rating":
        rows[0][8] = "3.5"
    elif case == "missing domain":
        for row in rows:
            if row[1] == "理科":
                row[1] = "数学"
    elif case == "negated domains":
        for row in rows:
            row[1] = "理科なし・数学なし・行事なし"
    elif case == "missing acoustic":
        for row in rows:
            if row[2] == "後方":
                row[2] = "雑音"
    elif case == "negated acoustics":
        for row in rows:
            row[2] = "後方ではない・雑音なし・チャイムなし"
    elif case == "Chinese average below threshold":
        for number, row in enumerate(rows, start=1):
            row[8] = "4" if number <= 14 else "3"
    else:  # pragma: no cover - protects the table above from silent typos.
        raise AssertionError(case)

    result = validate_quality_tsv(
        _write_quality_tsv(tmp_path, rows, header=header)
    )

    assert result["passed"] is False
    assert result["checks"][failed_check] is False


def test_quality_evidence_shell_output_is_stable_for_the_wizard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_quality_tsv(tmp_path, _quality_rows())

    assert quality_evidence_main([str(path), "--format", "shell"]) == 0

    assert capsys.readouterr().out.rstrip("\n").split("\t") == [
        "true",
        "30",
        "30",
        "30",
        "4.00",
        "30",
        "3.50",
        "30",
        "30",
        "3",
        "3",
    ]


def test_quality_evidence_shell_output_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = _quality_rows()
    rows.pop()
    path = _write_quality_tsv(tmp_path, rows)

    assert quality_evidence_main([str(path), "--format", "shell"]) == 1

    assert capsys.readouterr().out.split("\t", 1)[0] == "false"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_package_fixture(tmp_path: Path) -> dict[str, object]:
    field_date = "2026-09-08"
    evidence_dir = tmp_path / "docs" / "field" / "evidence" / field_date
    evidence_dir.mkdir(parents=True)
    tsv = _write_quality_tsv(evidence_dir, _quality_rows())

    audio = evidence_dir / "qual-01" / "lesson.wav"
    audio.parent.mkdir(parents=True)
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\0\0" * 8000 * 30)

    transcript = evidence_dir / "qual-01" / "transcript.jsonl"
    transcript_rows = []
    quality_rows = _quality_rows()
    for number, row in enumerate(quality_rows, start=1):
        transcript_rows.append(
            {
                "seq": number,
                "created_at": f"2026-09-08T10:00:{number - 1:02d}+09:00",
                "t_start": float(number - 1),
                "t_end": float(number - 1) + 0.8,
                "ja": row[4],
                "asr_ms": 100,
                "translations": {
                    "en": {"text": row[5], "engine": "hy-mt2", "mt_ms": 50},
                    "zh": {"text": row[7], "engine": "hy-mt2", "mt_ms": 60},
                },
            }
        )
    transcript.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in transcript_rows),
        encoding="utf-8",
        newline="\n",
    )

    lesson_attestation = evidence_dir / "qual-01" / "lesson-attestation.pdf"
    lesson_attestation.write_bytes(b"redacted lesson attestation")
    evaluators = []
    roles = (
        ("eval-ja", "reference_transcriber", "ja", "native_speaker"),
        ("eval-en", "meaning_evaluator", "en", "language_teacher"),
        ("eval-zh", "meaning_evaluator", "zh", "native_speaker"),
    )
    for evaluator_id, role, language, qualification in roles:
        qualification_path = evidence_dir / "qual-01" / f"{evaluator_id}-qualification.pdf"
        review_path = evidence_dir / "qual-01" / f"{evaluator_id}-review.pdf"
        qualification_path.write_bytes(f"{evaluator_id} qualification".encode())
        review_path.write_bytes(f"{evaluator_id} reviewed TSV hash".encode())
        evaluators.append(
            {
                "id": evaluator_id,
                "person_ref": f"person-{language}",
                "role": role,
                "language": language,
                "qualification_kind": qualification,
                "qualification_evidence": {
                    "path": qualification_path.relative_to(tmp_path).as_posix(),
                    "sha256": _hash(qualification_path),
                },
                "review_attestation": {
                    "path": review_path.relative_to(tmp_path).as_posix(),
                    "sha256": _hash(review_path),
                },
                "review_started_at": "2026-09-08T11:00:00+09:00",
                "review_finished_at": "2026-09-08T11:30:00+09:00",
            }
        )

    provenance = evidence_dir / f"{field_date}-quality-provenance.json"
    evaluators.sort(key=lambda item: item["id"])
    payload = {
        "schema_version": 1,
        "kind": "qual-01-provenance",
        "field_date": field_date,
        "captured_at": "2026-09-08T12:00:00+09:00",
        "field_session_id": "a" * 64,
        "host_fingerprint_sha256": "b" * 64,
        "quality_tsv": {
            "path": tsv.relative_to(tmp_path).as_posix(),
            "sha256": _hash(tsv),
        },
        "sources": [
            {
                "id": "lesson-01",
                "domain": "mixed",
                "audio": {
                    "path": audio.relative_to(tmp_path).as_posix(),
                    "sha256": _hash(audio),
                    "size_bytes": audio.stat().st_size,
                    "duration_ms": 30000,
                    "media_type": "audio/wav",
                },
                "transcript": {
                    "path": transcript.relative_to(tmp_path).as_posix(),
                    "sha256": _hash(transcript),
                },
                "recorded_started_at": "2026-09-08T09:30:00+09:00",
                "recorded_finished_at": "2026-09-08T09:30:30+09:00",
                "processing_started_at": "2026-09-08T10:00:00+09:00",
                "processing_finished_at": "2026-09-08T10:01:00+09:00",
                "lesson_attestation": {
                    "path": lesson_attestation.relative_to(tmp_path).as_posix(),
                    "sha256": _hash(lesson_attestation),
                },
            }
        ],
        "evaluators": evaluators,
        "segments": [
            {
                "row_no": number,
                "source_id": "lesson-01",
                "start_ms": (number - 1) * 1000,
                "end_ms": (number - 1) * 1000 + 800,
                "transcript_seq": number,
                "domain": ("science", "mathematics", "event_notice")[(number - 1) % 3],
                "acoustic_condition": ("rear", "noise", "chime")[(number - 1) % 3],
                "reference_evaluator_id": "eval-ja",
                "english_evaluator_id": "eval-en",
                "chinese_evaluator_id": "eval-zh",
            }
            for number in range(1, 31)
        ],
    }
    provenance.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest_files = {
        path.relative_to(tmp_path).as_posix(): _hash(path)
        for path in evidence_dir.rglob("*")
        if path.is_file()
    }
    return {
        "tsv_path": tsv,
        "provenance_path": provenance,
        "root": tmp_path,
        "field_date": field_date,
        "expected_session_id": "a" * 64,
        "expected_host_fingerprint_sha256": "b" * 64,
        "source_snapshot_start_at": "2026-09-08T09:00:00+09:00",
        "source_snapshot_end_at": "2026-09-08T13:00:00+09:00",
        "field_bundle_captured_at": "2026-09-08T14:00:00+09:00",
        "manifest_files": manifest_files,
    }


def test_quality_package_binds_thirty_source_segments_transcript_and_evaluators(
    tmp_path: Path,
) -> None:
    result = validate_quality_package(**_write_package_fixture(tmp_path))

    assert result["passed"] is True
    assert result["metrics"]["mapped_segment_count"] == 30
    assert result["metrics"]["transcript_bound_row_count"] == 30
    assert result["metrics"]["evaluator_assignment_count"] == 90
    assert result["checks"][
        "ninety_evaluator_assignments_declared_with_evidence"
    ] is True
    assert "ninety_evaluator_assignments_qualified" not in result["checks"]
    assert result["scope"]["source_segment_bounds_and_uniqueness_checked"] is True
    assert result["scope"]["real_lesson_origin_checked"] is False
    assert result["scope"]["evaluator_identity_checked"] is False
    assert result["scope"]["human_meaning_judgment_checked"] is False


def test_quality_package_witness_rejects_same_size_aba_with_restored_mtime(
    tmp_path: Path,
) -> None:
    arguments = _write_package_fixture(tmp_path)
    result = validate_quality_package(**arguments)
    assert isinstance(result, quality_evidence.ValidatedQualityPackage)
    target = arguments["provenance_path"]
    assert isinstance(target, Path)
    before = target.stat()
    original = target.read_bytes()
    replacement = bytes(byte ^ 1 for byte in original)
    target.write_bytes(replacement)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    target.write_bytes(original)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    with pytest.raises(quality_evidence.QualityEvidenceError, match="changed"):
        quality_evidence.recheck_quality_validation_witness(
            result.validation_witness
        )


def test_quality_package_rejects_old_evidence_relabelled_to_a_new_field_date(
    tmp_path: Path,
) -> None:
    arguments = _write_package_fixture(tmp_path)
    arguments["source_snapshot_start_at"] = "2026-09-07T09:00:00+09:00"

    result = validate_quality_package(**arguments)

    assert result["passed"] is False
    assert "local date does not match field_date" in result["error"]
    assert result["checks"][
        "ninety_evaluator_assignments_declared_with_evidence"
    ] is False


def _rewrite_package_provenance(
    arguments: dict[str, object], mutate,
) -> dict[str, object]:
    provenance = arguments["provenance_path"]
    root = arguments["root"]
    assert isinstance(provenance, Path)
    assert isinstance(root, Path)
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    mutate(payload)
    provenance.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest = arguments["manifest_files"]
    assert isinstance(manifest, dict)
    manifest[provenance.relative_to(root).as_posix()] = _hash(provenance)
    return arguments


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        (
            "wrong session",
            lambda payload: payload.__setitem__("field_session_id", "c" * 64),
        ),
        (
            "wrong WAV duration",
            lambda payload: payload["sources"][0]["audio"].__setitem__(
                "duration_ms", 29999
            ),
        ),
        (
            "overlapping source segments",
            lambda payload: payload["segments"][1].update(
                {"start_ms": 700, "end_ms": 1800}
            ),
        ),
        (
            "unknown evaluator",
            lambda payload: payload["segments"][0].__setitem__(
                "chinese_evaluator_id", "unknown"
            ),
        ),
        (
            "unsupported Chinese qualification",
            lambda payload: next(
                item for item in payload["evaluators"] if item["language"] == "zh"
            ).__setitem__("qualification_kind", "certified_proficiency"),
        ),
        (
            "review before processing completed",
            lambda payload: next(
                item for item in payload["evaluators"] if item["language"] == "en"
            ).update(
                {
                    "review_started_at": "2026-09-08T10:00:30+09:00",
                    "review_finished_at": "2026-09-08T10:00:45+09:00",
                }
            ),
        ),
        (
            "recording before the sealed field session",
            lambda payload: payload["sources"][0].update(
                {
                    "recorded_started_at": "2026-09-08T08:59:00+09:00",
                    "recorded_finished_at": "2026-09-08T08:59:30+09:00",
                }
            ),
        ),
        (
            "provenance after end snapshot",
            lambda payload: payload.__setitem__(
                "captured_at", "2026-09-08T13:00:01+09:00"
            ),
        ),
    ],
)
def test_quality_package_rejects_false_provenance_claims(
    tmp_path: Path, case: str, mutate
) -> None:
    arguments = _rewrite_package_provenance(
        _write_package_fixture(tmp_path), mutate
    )

    result = validate_quality_package(**arguments)

    assert result["passed"] is False, case
    assert result["error"]


def test_quality_package_uses_strict_json_for_provenance(tmp_path: Path) -> None:
    arguments = _write_package_fixture(tmp_path)
    provenance = arguments["provenance_path"]
    root = arguments["root"]
    manifest = arguments["manifest_files"]
    assert isinstance(provenance, Path)
    assert isinstance(root, Path)
    assert isinstance(manifest, dict)
    text = provenance.read_text(encoding="utf-8")
    provenance.write_text(
        text.replace(
            '"kind": "qual-01-provenance",',
            '"kind": "qual-01-provenance",\n  "kind": "qual-01-provenance",',
            1,
        ),
        encoding="utf-8",
    )
    manifest[provenance.relative_to(root).as_posix()] = _hash(provenance)

    result = validate_quality_package(**arguments)

    assert result["passed"] is False
    assert "duplicated" in result["error"]


def test_quality_package_uses_strict_jsonl_and_binds_translated_text(
    tmp_path: Path,
) -> None:
    arguments = _write_package_fixture(tmp_path)
    provenance = arguments["provenance_path"]
    root = arguments["root"]
    manifest = arguments["manifest_files"]
    assert isinstance(provenance, Path)
    assert isinstance(root, Path)
    assert isinstance(manifest, dict)
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    transcript = root / payload["sources"][0]["transcript"]["path"]
    records = transcript.read_text(encoding="utf-8").splitlines()
    first = json.loads(records[0])
    first["translations"]["zh"]["text"] = "forged translation"
    records[0] = json.dumps(first, ensure_ascii=False)
    transcript.write_text(
        "\n".join(records) + "\n", encoding="utf-8", newline="\n"
    )
    payload["sources"][0]["transcript"]["sha256"] = _hash(transcript)
    provenance.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest[transcript.relative_to(root).as_posix()] = _hash(transcript)
    manifest[provenance.relative_to(root).as_posix()] = _hash(provenance)

    result = validate_quality_package(**arguments)

    assert result["passed"] is False
    assert "sealed transcript" in result["error"]


def test_quality_package_rejects_an_unmanifested_nested_attestation(
    tmp_path: Path,
) -> None:
    arguments = _write_package_fixture(tmp_path)
    provenance = arguments["provenance_path"]
    manifest = arguments["manifest_files"]
    assert isinstance(provenance, Path)
    assert isinstance(manifest, dict)
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    relative = payload["evaluators"][0]["review_attestation"]["path"]
    del manifest[relative]

    result = validate_quality_package(**arguments)

    assert result["passed"] is False
    assert "outer manifest" in result["error"]


@pytest.mark.parametrize(
    "invalid_path",
    [
        "docs/field/evidence/2026-09-08/qual-01/lesson\n.wav",
        "docs/field/evidence/2026-09-08/qual-01:stream/lesson.wav",
    ],
)
def test_quality_package_rejects_control_or_colon_artifact_paths(
    tmp_path: Path, invalid_path: str
) -> None:
    arguments = _write_package_fixture(tmp_path)
    _rewrite_package_provenance(
        arguments,
        lambda payload: payload["sources"][0]["audio"].__setitem__(
            "path", invalid_path
        ),
    )

    result = validate_quality_package(**arguments)

    assert result["passed"] is False
    assert "canonical POSIX relative path" in result["error"]


def test_quality_package_rejects_a_junction_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _write_package_fixture(tmp_path)
    real_isjunction = getattr(quality_evidence.os.path, "isjunction", None)

    def fake_isjunction(path: object) -> bool:
        if Path(path).name == "qual-01":
            return True
        return bool(real_isjunction(path)) if real_isjunction is not None else False

    monkeypatch.setattr(quality_evidence.os.path, "isjunction", fake_isjunction)

    result = validate_quality_package(**arguments)

    assert result["passed"] is False
    assert "link or junction" in result["error"]


def test_quality_package_handles_a_huge_json_number_fail_closed(
    tmp_path: Path,
) -> None:
    arguments = _write_package_fixture(tmp_path)
    root = arguments["root"]
    provenance = arguments["provenance_path"]
    manifest = arguments["manifest_files"]
    assert isinstance(root, Path)
    assert isinstance(provenance, Path)
    assert isinstance(manifest, dict)
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    transcript = root / payload["sources"][0]["transcript"]["path"]
    records = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
    records[0]["t_start"] = 10**400
    transcript.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
        newline="\n",
    )
    payload["sources"][0]["transcript"]["sha256"] = _hash(transcript)
    provenance.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest[transcript.relative_to(root).as_posix()] = _hash(transcript)
    manifest[provenance.relative_to(root).as_posix()] = _hash(provenance)

    result = validate_quality_package(**arguments)

    assert result["passed"] is False
    assert "OverflowError" not in result["error"]


def test_quality_package_rejects_truncated_wav_payload(
    tmp_path: Path,
) -> None:
    arguments = _write_package_fixture(tmp_path)
    root = arguments["root"]
    provenance = arguments["provenance_path"]
    manifest = arguments["manifest_files"]
    assert isinstance(root, Path)
    assert isinstance(provenance, Path)
    assert isinstance(manifest, dict)
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    audio_binding = payload["sources"][0]["audio"]
    audio = root / audio_binding["path"]
    audio.write_bytes(audio.read_bytes()[:-100])
    audio_binding["sha256"] = _hash(audio)
    audio_binding["size_bytes"] = audio.stat().st_size
    provenance.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest[audio.relative_to(root).as_posix()] = _hash(audio)
    manifest[provenance.relative_to(root).as_posix()] = _hash(provenance)

    result = validate_quality_package(**arguments)

    assert result["passed"] is False
    assert "truncated" in result["error"]


def test_quality_package_hashes_and_parses_the_same_provenance_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _write_package_fixture(tmp_path)
    provenance = arguments["provenance_path"]
    root = arguments["root"]
    manifest = arguments["manifest_files"]
    assert isinstance(provenance, Path)
    assert isinstance(root, Path)
    assert isinstance(manifest, dict)
    valid_bytes = provenance.read_bytes()
    payload = json.loads(valid_bytes)
    payload["field_session_id"] = "c" * 64
    provenance.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest[provenance.relative_to(root).as_posix()] = _hash(provenance)
    original_read_bytes = Path.read_bytes

    def swapped_read_bytes(path: Path) -> bytes:
        if path == provenance:
            return valid_bytes
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", swapped_read_bytes)

    result = validate_quality_package(**arguments)

    assert result["passed"] is False
    assert "outer manifest" in result["error"]


def test_quality_package_rejects_hardlinked_evaluator_artifacts(
    tmp_path: Path,
) -> None:
    arguments = _write_package_fixture(tmp_path)
    provenance = arguments["provenance_path"]
    root = arguments["root"]
    manifest = arguments["manifest_files"]
    assert isinstance(provenance, Path)
    assert isinstance(root, Path)
    assert isinstance(manifest, dict)
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    evaluator = payload["evaluators"][0]
    qualification = root / evaluator["qualification_evidence"]["path"]
    review = root / evaluator["review_attestation"]["path"]
    review.unlink()
    try:
        os.link(qualification, review)
    except OSError as exc:
        pytest.skip(f"hardlinks are unavailable on this test filesystem: {exc}")
    evaluator["review_attestation"]["sha256"] = _hash(review)
    provenance.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest[review.relative_to(root).as_posix()] = _hash(review)
    manifest[provenance.relative_to(root).as_posix()] = _hash(provenance)

    result = validate_quality_package(**arguments)

    assert result["passed"] is False
    assert "aliases a previously bound artifact" in result["error"]


def _write_field_snapshots(arguments: dict[str, object]) -> tuple[Path, Path]:
    root = arguments["root"]
    field_date = arguments["field_date"]
    assert isinstance(root, Path)
    assert isinstance(field_date, str)
    evidence_dir = root / "docs" / "field" / "evidence" / field_date
    source_hashes = {"scripts/quality_evidence.py": "d" * 64}
    source_revisions = {
        "files": {
            "scripts/quality_evidence.py": {
                "dev": 1,
                "ino": 2,
                "size": 3,
                "mtime_ns": 4,
                "ctime_ns": 5,
            }
        },
        "directories": {
            "scripts": {
                "dev": 1,
                "ino": 6,
                "size": 7,
                "mtime_ns": 8,
                "ctime_ns": 9,
            }
        },
    }
    source_snapshot_sha256 = quality_evidence._source_snapshot_sha256(
        source_hashes, source_revisions
    )
    start = evidence_dir / "school-source-start.json"
    start_payload = {
        "schema_version": quality_evidence.SOURCE_SNAPSHOT_SCHEMA_VERSION,
        "kind": "field-source-snapshot",
        "field_date": field_date,
        "phase": "start",
        "captured_at": arguments["source_snapshot_start_at"],
        "host_fingerprint_sha256": arguments[
            "expected_host_fingerprint_sha256"
        ],
        "session_id": arguments["expected_session_id"],
        "predecessor_sha256": None,
        "source_hashes": source_hashes,
        "source_revisions": source_revisions,
        "source_snapshot_sha256": source_snapshot_sha256,
    }
    start.write_text(json.dumps(start_payload, indent=2) + "\n", encoding="utf-8")
    end = evidence_dir / "school-source-end.json"
    end_payload = {
        **start_payload,
        "phase": "end",
        "captured_at": arguments["source_snapshot_end_at"],
        "predecessor_sha256": _hash(start),
    }
    end.write_text(json.dumps(end_payload, indent=2) + "\n", encoding="utf-8")
    return start, end


def _write_outer_manifest(arguments: dict[str, object]) -> Path:
    root = arguments["root"]
    field_date = arguments["field_date"]
    assert isinstance(root, Path)
    assert isinstance(field_date, str)
    evidence_dir = root / "docs" / "field" / "evidence" / field_date
    files = sorted(
        (path for path in evidence_dir.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix().encode("utf-8"),
    )
    manifest = root / "docs" / "field" / f"{field_date}-evidence-sha256.txt"
    manifest.write_text(
        "".join(
            f"{_hash(path)}  {path.relative_to(root).as_posix()}\n" for path in files
        ),
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def test_quality_package_cli_derives_field_binding_from_strict_snapshots(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = _write_package_fixture(tmp_path)
    start, end = _write_field_snapshots(arguments)

    exit_code = quality_evidence_main(
        [
            "package",
            str(arguments["tsv_path"]),
            str(arguments["provenance_path"]),
            "--root",
            str(arguments["root"]),
            "--field-date",
            str(arguments["field_date"]),
            "--source-snapshot-start",
            str(start),
            "--source-snapshot-end",
            str(end),
            "--field-bundle-captured-at",
            str(arguments["field_bundle_captured_at"]),
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True


def test_quality_package_cli_matches_optional_outer_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = _write_package_fixture(tmp_path)
    start, end = _write_field_snapshots(arguments)
    manifest = _write_outer_manifest(arguments)

    exit_code = quality_evidence_main(
        [
            "package",
            str(arguments["tsv_path"]),
            str(arguments["provenance_path"]),
            "--root",
            str(arguments["root"]),
            "--field-date",
            str(arguments["field_date"]),
            "--source-snapshot-start",
            str(start),
            "--source-snapshot-end",
            str(end),
            "--manifest",
            str(manifest),
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["passed"] is True
    assert result["scope"]["manifest_references_checked"] is True


def test_quality_package_cli_rechecks_all_artifacts_after_formatting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = _write_package_fixture(tmp_path)
    start, end = _write_field_snapshots(arguments)
    manifest = _write_outer_manifest(arguments)
    provenance = arguments["provenance_path"]
    assert isinstance(provenance, Path)
    real_summary = quality_evidence._package_shell_summary

    def tampering_summary(result: dict[str, object]) -> str:
        rendered = real_summary(result)
        before = provenance.stat()
        original = provenance.read_bytes()
        provenance.write_bytes(bytes(byte ^ 1 for byte in original))
        os.utime(provenance, ns=(before.st_atime_ns, before.st_mtime_ns))
        provenance.write_bytes(original)
        os.utime(provenance, ns=(before.st_atime_ns, before.st_mtime_ns))
        return rendered

    monkeypatch.setattr(quality_evidence, "_package_shell_summary", tampering_summary)
    exit_code = quality_evidence_main(
        [
            "package",
            str(arguments["tsv_path"]),
            str(provenance),
            "--root",
            str(arguments["root"]),
            "--field-date",
            str(arguments["field_date"]),
            "--source-snapshot-start",
            str(start),
            "--source-snapshot-end",
            str(end),
            "--field-bundle-captured-at",
            str(arguments["field_bundle_captured_at"]),
            "--manifest",
            str(manifest),
            "--format",
            "shell",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "changed" in captured.err


def test_quality_package_cli_rejects_a_broken_snapshot_chain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = _write_package_fixture(tmp_path)
    start, end = _write_field_snapshots(arguments)
    end_payload = json.loads(end.read_text(encoding="utf-8"))
    end_payload["predecessor_sha256"] = "e" * 64
    end.write_text(json.dumps(end_payload, indent=2) + "\n", encoding="utf-8")

    exit_code = quality_evidence_main(
        [
            "package",
            str(arguments["tsv_path"]),
            str(arguments["provenance_path"]),
            "--root",
            str(arguments["root"]),
            "--field-date",
            str(arguments["field_date"]),
            "--source-snapshot-start",
            str(start),
            "--source-snapshot-end",
            str(end),
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["passed"] is False
    assert "snapshot chain" in result["error"]


def test_quality_wizard_double_gates_shell_summary_and_source_evidence() -> None:
    wizard = (
        Path(__file__).resolve().parents[2] / "scripts" / "field-verification.sh"
    ).read_text(encoding="utf-8")
    phase_c6 = wizard.split('stage "Phase C-6', maxsplit=1)[1]
    validator_line = next(
        line
        for line in phase_c6.splitlines()
        if "quality_evidence.py" in line and "--format shell" in line
    )

    assert validator_line.lstrip().startswith("if QUALITY_SUMMARY=")
    assert "|| true" not in validator_line
    assert "QUALITY_STATUS=$?" in phase_c6
    assert '"$QUALITY_SUMMARY" != *$\'\\n\'*' in phase_c6
    assert "${#QUALITY_FIELDS[@]} == 11" in phase_c6
    for field in (
        "F_C6_ROWS",
        "F_C6_REQUIRED",
        "F_C6_EN_COUNT",
        "F_C6_ZH_COUNT",
        "F_C6_NUMBERS",
        "F_C6_DISTINCT",
    ):
        assert f'"${field}" == "30"' in phase_c6
    for field in ("F_C6_DOMAIN_COVERAGE", "F_C6_ACOUSTIC_COVERAGE"):
        assert f'"${field}" == "3"' in phase_c6
    assert (
        '"$F_C6_SOURCE_CANONICAL" != "$F_C6_FILE_CANONICAL"' in phase_c6
    )
