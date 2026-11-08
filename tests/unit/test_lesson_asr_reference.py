import wave
from pathlib import Path

import numpy as np
import pytest

from scripts import lesson_asr_reference as reference
from scripts.lesson_asr_eval import CandidateRun, CandidateSegment, audio_digest, load_audio, score


def candidate(text: str, *, digest: str = "a" * 64, end: int = 5) -> CandidateRun:
    return CandidateRun(audio_sha256=digest, segments={
        "0": CandidateSegment(start_sample=2, end_sample=end, text=text),
    })


def test_medoid_minimizes_character_edit_distance_and_breaks_ties_by_text():
    assert reference.medoid(["地理学", "地理", "地形"]) == "地理"
    assert reference.medoid(["地理", "世界"]) == reference.medoid(["世界", "地理"])
    assert reference.medoid(["はい", "", ""]) == ""


@pytest.mark.parametrize("text", ["日本語。", "", "1行目\n```\n2行目"])
def test_unedited_draft_round_trips_the_proposal(text):
    draft = reference.render_draft({"候補A": candidate(text), "候補B": candidate(text)})
    assert reference.extract_reference(draft) == ({"0": text}, [])


def test_draft_highlights_disagreements_and_reports_only_edited_segments():
    draft = reference.render_draft({
        "候補A": candidate("水半球です"),
        "候補B": candidate("北半球です"),
    })
    assert "<strong>水</strong>半球です" in draft
    assert "<strong>北</strong>半球です" in draft
    assert "[区間音声](audio/0.wav)" in draft
    original, _ = reference.extract_reference(draft)
    edited = draft.replace(f"```text\n{original['0']}\n", "```text\n地球です。\n")
    assert reference.extract_reference(edited) == ({"0": "地球です。"}, ["0"])


def test_empty_edited_reference_counts_hypothesis_as_insertions():
    draft = reference.render_draft({"候補A": candidate("誤認"), "候補B": candidate("誤認")})
    edited = draft.replace("```text\n誤認\n```", "```text\n```")
    actual, edits = reference.extract_reference(edited)
    assert (actual, edits) == ({"0": ""}, ["0"])
    assert score(actual | {"1": "地理"}, {"0": "誤認", "1": "地理"}, []) == (
        "CER=1.0000 S=0 D=0 I=2 N=2 terms=0/0"
    )


def test_candidate_text_is_displayed_as_literal_markdown():
    text = "[地理](https://example.test/)"
    draft = reference.render_draft({"候補A": candidate(text), "候補B": candidate(text)})
    assert "&#91;地理&#93;&#40;https://example&#46;test/&#41;" in draft
    assert reference.extract_reference(draft) == ({"0": text}, [])


@pytest.mark.parametrize("other", [candidate("地理", digest="b" * 64), candidate("地理", end=6)])
def test_draft_rejects_different_audio_or_segment_boundaries(other):
    with pytest.raises(ValueError, match="一致しません"):
        reference.render_draft({"候補A": candidate("地理"), "候補B": other})


def test_build_writes_the_exact_audio_slice_and_preserves_an_existing_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(reference, "outside_repo", Path.resolve)
    pcm = np.arange(8, dtype="<i2")
    audio = tmp_path / "lesson.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(pcm.tobytes())
    run = candidate("地理", digest=audio_digest(audio))
    data = tmp_path / "candidate.json"
    data.write_text(run.model_dump_json(), encoding="utf-8")
    output = tmp_path / "draft"
    candidates = [["候補A", str(data)], ["候補B", str(data)]]

    reference.build_draft(audio, candidates, output)

    with wave.open(str(output / "audio/0.wav"), "rb") as stream:
        assert stream.getframerate() == 16000
        assert stream.readframes(100) == load_audio(audio)[2:5].astype("<i2").tobytes()
    markdown = output / "draft.md"
    draft = markdown.read_text(encoding="utf-8").replace("```text\n地理", "```text\n世界地理")
    markdown.write_text(draft, encoding="utf-8")
    with pytest.raises(FileExistsError):
        reference.build_draft(audio, candidates, output)
    assert markdown.read_text(encoding="utf-8") == draft
    assert reference.extract_reference(draft) == ({"0": "世界地理"}, ["0"])
