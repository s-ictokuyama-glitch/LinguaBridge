"""授業動画の候補出力から確認用の下書きを作り、確定欄を --ref 用 JSON に変換する。"""

from __future__ import annotations

import argparse
import json
import re
import sys
import wave
from difflib import SequenceMatcher
from html import escape
from itertools import combinations
from pathlib import Path

from scripts.lesson_asr_eval import (
    SAMPLE_RATE, CandidateRun, load_candidate_audio, outside_repo,
)
from scripts.text_metrics import edit_counts

DRAFT_HEADER = "<!-- LinguaBridge reference v1 "
FINAL_MARKER = re.compile(r"^<!-- LinguaBridge final (\d+) -->\n", re.MULTILINE)
MARKDOWN_ENTITIES = {ord(char): f"&#{ord(char)};" for char in r"\`*_{}[]()#+-.!|~="}


def medoid(texts: list[str]) -> str:
    totals = [0] * len(texts)
    for first, second in combinations(range(len(texts)), 2):
        distance = edit_counts(texts[first], texts[second]).distance
        totals[first] += distance
        totals[second] += distance
    return min(zip(totals, texts))[1]


def inline_text(text: str) -> str:
    return escape(text).translate(MARKDOWN_ENTITIES).replace("\n", "<br>")


def highlight(text: str, peers: list[str]) -> str:
    differences: set[int] = set()
    for peer in peers:
        for operation, start, end, _, _ in SequenceMatcher(
            None, text, peer, autojunk=False
        ).get_opcodes():
            if operation != "equal":
                differences.update(range(start, end))
    marked = []
    cursor = 0
    for index in sorted(differences):
        if index > cursor:
            marked.append(inline_text(text[cursor:index]))
        if index == 0 or index - 1 not in differences:
            marked.append("<strong>")
        marked.append(inline_text(text[index]))
        if index + 1 not in differences:
            marked.append("</strong>")
        cursor = index + 1
    marked.append(inline_text(text[cursor:]))
    return "".join(marked) or "（空欄）"


def render_draft(runs: dict[str, CandidateRun]) -> str:
    if len(runs) < 2:
        raise ValueError("候補を二つ以上指定してください")
    first = next(iter(runs.values()))
    indices = [str(i) for i in range(len(first.segments))]
    if not indices or set(indices) != set(first.segments):
        raise ValueError("Segment 番号は 0 から連続している必要があります")
    for name, run in runs.items():
        if run.audio_sha256 != first.audio_sha256 or set(run.segments) != set(indices):
            raise ValueError(f"{name} の入力音声または Segment 番号が一致しません")
        for index in indices:
            expected = first.segments[index]
            actual = run.segments[index]
            if (actual.start_sample, actual.end_sample) != (expected.start_sample, expected.end_sample):
                raise ValueError(f"{name} の Segment {index} の音声区間が一致しません")

    baseline = {}
    blocks = []
    for index in indices:
        segment = first.segments[index]
        outputs = {name: run.segments[index].text for name, run in runs.items()}
        proposal = medoid(list(outputs.values()))
        baseline[index] = proposal
        ticks = max((len(run) for run in re.findall(r"`+", proposal)), default=0)
        fence = "`" * max(3, ticks + 1)
        candidate_lines = [
            f"- {inline_text(name)}　{highlight(text, [peer for label, peer in outputs.items() if label != name])}"
            for name, text in outputs.items()
        ]
        blocks.append("\n".join([
            f"## Segment {index}",
            "",
            f"{segment.start_sample / SAMPLE_RATE:.3f}〜{segment.end_sample / SAMPLE_RATE:.3f} 秒",
            f"[区間音声](audio/{index}.wav)",
            "",
            *candidate_lines,
            "",
            "確定文",
            f"<!-- LinguaBridge final {index} -->",
            f"{fence}text",
            proposal,
            fence,
        ]))
    metadata = json.dumps(baseline, ensure_ascii=False).replace("<", r"\u003c").replace(">", r"\u003e")
    return "\n\n".join([
        f"{DRAFT_HEADER}{metadata} -->\n# 正解文の下書き",
        "区間音声を聞いて「確定文」のコードブロック内だけを編集してください。",
        *blocks,
    ]) + "\n"


def extract_reference(draft: str) -> tuple[dict[str, str], list[str]]:
    header, _, body = draft.partition("\n")
    if not header.startswith(DRAFT_HEADER) or not header.endswith(" -->"):
        raise ValueError("正解文の下書きの先頭行が壊れています")
    baseline = json.loads(header[len(DRAFT_HEADER):-4])
    if not isinstance(baseline, dict) or not baseline:
        raise ValueError("正解文の下書きに Segment がありません")
    reference: dict[str, str] = {}
    cursor = 0
    while match := FINAL_MARKER.search(body, cursor):
        index = match[1]
        opening = re.match(r"(`{3,})text\n", body[match.end():])
        if index in reference or opening is None:
            raise ValueError(f"Segment {index} の確定欄が壊れています")
        start = match.end() + opening.end()
        closing = re.compile(r"^" + re.escape(opening[1]) + r"(?=\n|$)", re.MULTILINE).search(body, start)
        if closing is None:
            raise ValueError(f"Segment {index} の確定欄が閉じていません")
        reference[index] = body[start:closing.start()].removesuffix("\n")
        cursor = closing.end()
    if list(reference) != list(baseline):
        raise ValueError("確定欄の Segment 番号が下書きと一致しません")
    return reference, [index for index in reference if reference[index] != baseline[index]]


def build_draft(audio: Path, candidates: list[list[str]], output_dir: Path) -> None:
    output_dir = outside_repo(output_dir)
    runs = {
        name: CandidateRun.model_validate_json(Path(path).read_bytes())
        for name, path in candidates
    }
    if len(runs) != len(candidates):
        raise ValueError("候補名は重複させないでください")
    markdown = render_draft(runs)
    first = next(iter(runs.values()))
    pcm, digest = load_candidate_audio(audio)
    if digest != first.audio_sha256:
        raise ValueError("動画が候補の書き起こしに使った入力と一致しません")
    for segment in first.segments.values():
        if not segment.start_sample < segment.end_sample <= pcm.size:
            raise ValueError("Segment の音声区間が入力の範囲外です")

    output_dir.mkdir(parents=True, exist_ok=False)
    audio_dir = output_dir / "audio"
    audio_dir.mkdir()
    for index, segment in first.segments.items():
        with wave.open(str(audio_dir / f"{index}.wav"), "wb") as stream:
            stream.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            stream.writeframes(pcm[segment.start_sample:segment.end_sample].astype("<i2").tobytes())
    (output_dir / "draft.md").write_text(markdown, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="下書きと区間音声を新しいディレクトリに保存")
    build.add_argument("audio", type=Path)
    build.add_argument("--candidate", nargs=2, metavar=("NAME", "JSON"), action="append", required=True)
    build.add_argument("--out-dir", type=Path, required=True)
    extract = commands.add_parser("extract", help="下書きから確定欄を取り出す")
    extract.add_argument("draft", type=Path)
    extract.add_argument("--out", type=Path, required=True)
    extract.add_argument("--edits", type=Path, help="修正した Segment 番号の JSON 保存先")
    args = parser.parse_args()

    try:
        if args.command == "build":
            build_draft(args.audio, args.candidate, args.out_dir)
        else:
            reference, edited = extract_reference(args.draft.read_text(encoding="utf-8"))
            outputs: dict[Path, dict[str, str] | list[str]] = {outside_repo(args.out): reference}
            if args.edits:
                outputs[outside_repo(args.edits)] = edited
            if len(outputs) != (2 if args.edits else 1):
                raise ValueError("正解文と修正一覧には別々のファイルを指定してください")
            for path in outputs:
                if path.exists():
                    raise FileExistsError(path)
            for path, data in outputs.items():
                with path.open("x", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False, indent=2)
            print(json.dumps(edited))
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
