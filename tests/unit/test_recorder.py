"""授業記録の書き出し（server/recorder.py）のユニットテスト（#18）。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from server.caption_history import Translation, Utterance
from server.recorder import SessionRecorder

STARTED = datetime(2026, 9, 1, 10, 30, tzinfo=timezone.utc)

# JSONL エントリで許可されるキー（生徒情報が混入しないことの厳密チェック）
ALLOWED_ENTRY_KEYS = {"seq", "created_at", "t_start", "t_end", "ja", "asr_ms", "translations"}
ALLOWED_TR_KEYS = {"text", "engine", "mt_ms"}


def make_utterance(seq: int, ja: str, langs: dict[str, str]) -> Utterance:
    # created_at は固定（フォルダ名＝最初の発話時刻の決定性のため）
    utt = Utterance(
        seq=seq, t_start=float(seq), t_end=seq + 1.0, text_ja=ja, asr_ms=10, created_at=STARTED
    )
    for lang, text in langs.items():
        utt.translations[lang] = Translation(lang=lang, text=text, engine="fake", mt_ms=5)
    return utt


def test_empty_recorder_writes_nothing(tmp_path):
    rec = SessionRecorder(tmp_path, ["en", "zh"])
    assert not rec.has_entries
    assert rec.write() is None
    assert list(tmp_path.iterdir()) == []


def test_writes_jsonl_and_per_language_markdown(tmp_path):
    rec = SessionRecorder(tmp_path, ["en", "zh"])
    rec.add(make_utterance(1, "おはよう。", {"en": "[en] おはよう。", "zh": "[zh] おはよう。"}))
    rec.add(make_utterance(2, "光合成の話。", {"en": "[en] 光合成の話。"}))  # zhは無し

    out = rec.write()
    assert out is not None
    assert out.name == "2026-09-01_1030"
    assert (out / "transcript.jsonl").exists()
    assert (out / "transcript.ja.md").exists()
    assert (out / "transcript.en.md").exists()
    assert (out / "transcript.zh.md").exists()  # seq1にzh訳があるので生成される

    lines = (out / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    e0 = json.loads(lines[0])
    assert e0["seq"] == 1
    assert e0["ja"] == "おはよう。"
    assert e0["translations"]["en"]["text"] == "[en] おはよう。"
    assert e0["translations"]["en"]["engine"] == "fake"
    datetime.fromisoformat(e0["created_at"])  # ISO 形式であること

    ja_md = (out / "transcript.ja.md").read_text(encoding="utf-8")
    assert "おはよう。" in ja_md and "光合成の話。" in ja_md
    en_md = (out / "transcript.en.md").read_text(encoding="utf-8")
    assert "[en] おはよう。" in en_md and "[en] 光合成の話。" in en_md


def test_language_with_no_translation_gets_no_file(tmp_path):
    rec = SessionRecorder(tmp_path, ["en", "zh"])
    rec.add(make_utterance(1, "英語だけ。", {"en": "[en] 英語だけ。"}))  # zh訳は一度も無い
    out = rec.write()
    assert out is not None
    assert (out / "transcript.en.md").exists()
    assert not (out / "transcript.zh.md").exists()  # zh訳ゼロ → ファイル作らない


def test_no_student_or_connection_info_in_output(tmp_path):
    rec = SessionRecorder(tmp_path, ["en", "zh"])
    rec.add(make_utterance(1, "テスト。", {"en": "[en] テスト。"}))
    out = rec.write()
    assert out is not None
    entry = json.loads((out / "transcript.jsonl").read_text(encoding="utf-8").splitlines()[0])
    # キー集合が発話由来のものだけであること（client/ws/ip等が無い）
    assert set(entry.keys()) == ALLOWED_ENTRY_KEYS
    assert set(entry["translations"]["en"].keys()) == ALLOWED_TR_KEYS


def test_write_is_idempotent(tmp_path):
    rec = SessionRecorder(tmp_path, ["en"])
    rec.add(make_utterance(1, "一度きり。", {"en": "[en] 一度きり。"}))
    assert rec.write() is not None
    assert rec.write() is None  # 二度目は書かない


def test_uncapped_beyond_history_limit(tmp_path):
    # session.history は50件で丸められるが、recorderは全件保持する
    rec = SessionRecorder(tmp_path, ["en"])
    for i in range(1, 61):
        rec.add(make_utterance(i, f"発話{i}", {"en": f"[en] 発話{i}"}))
    out = rec.write()
    assert out is not None
    lines = (out / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 60


def recording_files(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in directory.iterdir()}


def assert_recorded_utterance(directory: Path, utterance: Utterance) -> None:
    entries = [
        json.loads(line)
        for line in (directory / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(entries) == 1
    assert entries[0]["ja"] == utterance.text_ja
    assert entries[0]["translations"] == {
        lang: {"text": tr.text, "engine": tr.engine, "mt_ms": tr.mt_ms}
        for lang, tr in utterance.translations.items()
    }
    texts = {"ja": utterance.text_ja}
    texts.update({lang: tr.text for lang, tr in utterance.translations.items()})
    assert set(recording_files(directory)) == {
        "transcript.jsonl", *(f"transcript.{lang}.md" for lang in texts)
    }
    for lang, text in texts.items():
        lines = (directory / f"transcript.{lang}.md").read_text(encoding="utf-8").splitlines()
        assert lines[2:] == [f"- **{utterance.created_at:%H:%M:%S}** {text}"]


@pytest.mark.parametrize("failure_stage", ["mkdir", "jsonl", "markdown"])
def test_write_retries_after_io_failure_without_mixing_sessions(tmp_path, monkeypatch, failure_stage):
    output_root = tmp_path / "recordings"
    rec = SessionRecorder(output_root, ["en", "zh"])
    first = make_utterance(1, "最初の授業。", {"en": "First lesson.", "zh": "第一堂课。"})
    rec.add(first)
    original_open = Path.open
    original_write_text = Path.write_text

    def fail_mkdir(path, *args, **kwargs):
        raise OSError("injected mkdir failure")

    def fail_jsonl(path, *args, **kwargs):
        if path.name == "transcript.jsonl":
            with original_open(path, *args, **kwargs) as stream:
                stream.write("partial JSONL")
            raise OSError("injected JSONL failure")
        return original_open(path, *args, **kwargs)

    def fail_markdown(path, *args, **kwargs):
        if path.name == "transcript.zh.md":
            original_write_text(path, "partial Markdown", encoding="utf-8")
            raise OSError("injected Markdown failure")
        return original_write_text(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        if failure_stage == "mkdir":
            patch.setattr(Path, "mkdir", fail_mkdir)
        elif failure_stage == "jsonl":
            patch.setattr(Path, "open", fail_jsonl)
        else:
            patch.setattr(Path, "write_text", fail_markdown)
        with pytest.raises(OSError, match="injected"):
            rec.write()

    other = SessionRecorder(output_root, ["en", "zh"])
    second = make_utterance(1, "次の授業。", {"en": "Second lesson.", "zh": "第二堂课。"})
    other.add(second)
    other_directory = other.write()
    assert other_directory is not None
    other_files = recording_files(other_directory)

    directory = rec.write()
    assert directory is not None
    assert directory != other_directory
    assert_recorded_utterance(directory, first)
    assert_recorded_utterance(other_directory, second)
    assert recording_files(other_directory) == other_files
    saved_files = recording_files(directory)
    assert rec.write() is None
    assert recording_files(directory) == saved_files
    assert set(output_root.iterdir()) == {directory, other_directory}


def test_same_minute_sessions_preserve_all_language_files(tmp_path):
    directories = []
    snapshots = []
    for number in range(3):
        rec = SessionRecorder(tmp_path, ["en", "zh"])
        utterance = make_utterance(
            1, f"授業{number}。", {"en": f"Lesson {number}.", "zh": f"第{number}堂课。"}
        )
        rec.add(utterance)
        directory = rec.write()
        assert directory is not None
        expected_name = "2026-09-01_1030" + (f"_{number}" if number else "")
        assert directory.name == expected_name
        assert_recorded_utterance(directory, utterance)
        directories.append(directory)
        snapshots.append(recording_files(directory))
        for previous, saved_files in zip(directories, snapshots):
            assert recording_files(previous) == saved_files
    assert set(tmp_path.iterdir()) == set(directories)
