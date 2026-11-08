"""Exhaustively check PCM conservation across bounded VoiceSegmenter splits."""

from __future__ import annotations

import argparse
import itertools
import json
import runpy
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
VAD_SOURCE = ROOT / "server/audio/vad.py"
SYNC_COMMIT = "5381edb92062d1a35972b3a7dee8d387c5b0aa1b"
AMPLITUDE = {"s": 0, "q": 1000, "v": 2000}
SAMPLE_RATE = 8
FRAME_SAMPLES = 2
LIMIT_SAMPLES = SAMPLE_RATE
MAX_FRAMES = 8


def make_pcm(frames: str) -> np.ndarray:
    pcm = []
    for index, frame in enumerate(frames):
        amplitude = AMPLITUDE[frame] + 2 * index
        pcm.extend((amplitude, -amplitude - 1))
    return np.array(pcm, dtype=np.int16)


def check_case(vad, frames: str, cuts: tuple[int, ...], *, tail: bool) -> dict | None:
    pcm = make_pcm(frames)
    wire_pcm = np.append(pcm, np.int16(3000)) if tail else pcm
    segmenter = vad["VoiceSegmenter"](
        vad["EnergyVAD"](), sample_rate=SAMPLE_RATE, frame_ms=250,
        max_utterance_s=1, min_silence_ms=750, pre_roll_ms=500,
    )
    segment_type = vad["Segment"]
    segments = []
    for chunk in np.split(wire_pcm, cuts):
        segments.extend(event for event in segmenter.feed(chunk) if isinstance(event, segment_type))
    final = segmenter.flush()
    if final is not None:
        segments.append(final)
    duplicate = segmenter.flush()

    consumed = 0
    for index, segment in enumerate(segments):
        end = consumed + segment.pcm.size
        expected_start = consumed / SAMPLE_RATE
        expected_pcm = pcm[consumed:end]
        if segment.t_start != expected_start or not np.array_equal(segment.pcm, expected_pcm):
            return dict(
                property="pcm_partition", segment=index + 1,
                expected=dict(start=expected_start, pcm=expected_pcm.tolist()),
                actual=dict(start=segment.t_start, pcm=segment.pcm.tolist()),
            )
        if not 0 < segment.pcm.size <= LIMIT_SAMPLES or segment.pcm.size % FRAME_SAMPLES:
            return dict(property="segment_size", segment=index + 1,
                        expected=list(range(FRAME_SAMPLES, LIMIT_SAMPLES + 1, FRAME_SAMPLES)),
                        actual=segment.pcm.size)
        consumed = end
    if consumed != pcm.size:
        return dict(property="pcm_partition", expected=pcm.tolist(), actual=pcm[:consumed].tolist())
    if duplicate is not None:
        return dict(property="pcm_partition", expected="second flush returns None",
                    actual=duplicate.pcm.tolist())
    return None


def check(vad) -> dict:
    scenarios = 0
    inputs_at_or_above_limit = 0
    for size in range(1, MAX_FRAMES + 1):
        for symbols in itertools.product(AMPLITUDE, repeat=size):
            frames = "".join(symbols)
            if frames[0] == "s" or frames[-1] == "s" or "sss" in frames:
                continue
            if size * FRAME_SAMPLES >= LIMIT_SAMPLES:
                inputs_at_or_above_limit += 1
            for tail in (False, True):
                samples = size * FRAME_SAMPLES + tail
                interior = range(1, samples)
                partitions = [(), *((cut,) for cut in interior)]
                if samples > 2:
                    partitions.append(tuple(interior))
                for cuts in partitions:
                    scenarios += 1
                    violation = check_case(vad, frames, cuts, tail=tail)
                    if violation is not None:
                        return dict(status="FAIL", scenarios=scenarios, violation=dict(
                            frames=frames, cuts=cuts, tail=tail, **violation,
                        ))
    return dict(status="PASS", scenarios=scenarios, inputs_at_or_above_limit=inputs_at_or_above_limit)


def mutation_check(vad) -> list[dict]:
    mutations = (
        ("drop_carry", "pcm_partition", "vvsvv", (),
         "self._frames = carried", "self._frames = []"),
        ("duplicate_carry", "pcm_partition", "vvsvv", (),
         "carried = self._frames[keep:]", "carried = self._frames[keep - 1:]"),
        ("drop_chunk_remainder", "pcm_partition", "vv", (1,),
         "np.concatenate([self._pending, pcm]) if self._pending.size else pcm", "pcm"),
        ("drop_pre_roll", "pcm_partition", "vvvqsv", (),
         "opening = list(self._pre_roll)", "opening = [frame]"),
        ("raise_split_limit", "segment_size", "vvvvv", (),
         "self._offset - self._utt_start_sample < self._max_samples",
         "self._offset - self._utt_start_sample < self._max_samples * 2"),
        ("drop_flush_output", "pcm_partition", "v", (),
         "return segment\n\n    def reset", "return None\n\n    def reset"),
    )
    source = VAD_SOURCE.read_text(encoding="utf-8")
    results = []
    for name, property_name, frames, cuts, original, broken in mutations:
        if check_case(vad, frames, cuts, tail=False) is not None:
            raise RuntimeError(f"mutation witness fails on original code: {name}")
        if source.count(original) != 1:
            raise RuntimeError(f"mutation site changed: {name}")
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="vad-segmentation-", suffix=".py",
            delete_on_close=False,
        ) as scratch:
            scratch.write(source.replace(original, broken))
            scratch.close()
            mutant = runpy.run_path(scratch.name)
        violation = check_case(mutant, frames, cuts, tail=False)
        if violation is None or violation["property"] != property_name:
            raise RuntimeError(f"mutation missed {property_name}: {name}: {violation}")
        results.append(dict(name=name, frames=frames, cuts=cuts, detected=True,
                            violation=violation))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutation-check", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from server.audio import vad

    result = check(vars(vad))
    if args.mutation_check and result["status"] == "PASS":
        result["mutations"] = mutation_check(vars(vad))
    print(json.dumps(dict(sync_commit=SYNC_COMMIT, **result), indent=2))
    return int(result["status"] == "FAIL")


if __name__ == "__main__":
    raise SystemExit(main())
