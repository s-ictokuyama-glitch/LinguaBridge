"""拡張ベンチコーパスを組み立てる（イシュー#22）。

`scripts/make_fixture_audio.ps1` が SAPI で **セグメント単位** に合成した一時WAVを受け取り、
前後の無音をトリムしてから指定どおりの長さの無音を挟んで連結する。
間を合成側（SSML の break）に任せず自前で挿入するのが肝で、これによって
「どの時刻にどれだけのギャップがあるか」が厳密な正解になり、区切りの機械判定ができる。

定義は tests/fixtures/ja_corpus.json（コミット対象）。
出力は tests/fixtures/ja_ext/（.gitignore 済み・再生成可能）:
  <id>.wav     16kHz mono PCM16
  index.json   既存10文を含む全クリップの正解テキスト・ギャップ時刻・期待turn数

使い方（通常は make_fixture_audio.ps1 から呼ばれる）:
  python scripts/build_fixture_corpus.py --segments-dir <一時ディレクトリ>
"""

from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = ROOT / "tests" / "fixtures"
CORPUS_FILE = FIXTURE_ROOT / "ja_corpus.json"
SENTENCES_FILE = FIXTURE_ROOT / "ja_sentences.txt"
BASE_DIR = FIXTURE_ROOT / "ja"
OUT_DIR = FIXTURE_ROOT / "ja_ext"

# 無音トリムのパラメータ。SAPI は発話の前後に無音を付けるので、そのまま連結すると
# 意図した間より長いギャップになってしまう。
TRIM_FRAME_MS = 10
TRIM_REL_THRESHOLD = 0.005  # ピーク比
TRIM_ABS_THRESHOLD = 3 / 32768  # PCM16 の量子化ノイズより少し上
TRIM_KEEP_MS = 30  # 立ち上がりを削らないための余白

# セグメント内部の無音の検出。SAPI は読点で 300〜700ms の無音を勝手に入れるので、
# 台本に書いた間だけを正解にすると、区切りベンチが『不自然な分割』を数え落とす。
# 実際に鳴っている無音はすべてアノテーションに載せる。
INTERNAL_SILENCE_FRAME_MS = 10
INTERNAL_SILENCE_REL_THRESHOLD = 0.01  # ピーク比
INTERNAL_SILENCE_MIN_MS = 150  # これ未満で切る endpointer は現実的に無い


def read_wav(path: Path) -> np.ndarray:
    """16kHz mono PCM16 を float32 [-1, 1) で読む。ヘッダ長は決め打ちしない。"""
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(
                f"{path.name}: 16kHz mono PCM16 ではない"
                f"（{w.getframerate()}Hz/{w.getnchannels()}ch/{w.getsampwidth() * 8}bit）"
            )
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0


def write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    pcm = np.clip(audio, -1.0, 1.0 - 1 / 32768)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes((pcm * 32768.0).astype(np.int16).tobytes())


def frame_rms(audio: np.ndarray, frame: int) -> np.ndarray:
    n = audio.size // frame
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    return np.sqrt((audio[: n * frame].reshape(n, frame) ** 2).mean(axis=1))


def trim_silence(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """前後の無音を落とす。中間の無音には触れない。"""
    frame = sample_rate * TRIM_FRAME_MS // 1000
    rms = frame_rms(audio, frame)
    if rms.size == 0:
        return audio
    threshold = max(rms.max() * TRIM_REL_THRESHOLD, TRIM_ABS_THRESHOLD)
    active = np.flatnonzero(rms > threshold)
    if active.size == 0:
        return audio
    keep = sample_rate * TRIM_KEEP_MS // 1000
    start = max(0, active[0] * frame - keep)
    end = min(audio.size, (active[-1] + 1) * frame + keep)
    return audio[start:end]


def active_rms(audio: np.ndarray, sample_rate: int) -> float:
    """発話区間だけの RMS。長い無音を含む音源で SNR の分母がずれないようにする。"""
    frame = sample_rate * 20 // 1000
    rms = frame_rms(audio, frame)
    if rms.size == 0:
        return float(np.sqrt((audio**2).mean())) if audio.size else 0.0
    active = rms[rms > rms.max() * 0.1]
    if active.size == 0:
        active = rms
    return float(np.sqrt((active**2).mean()))


def steady_noise(
    n: int, sample_rate: int, lowpass_hz: float, rng: np.random.Generator
) -> np.ndarray:
    """空調・ファン相当の定常雑音。白色雑音を1次ローパスに通す。"""
    white = rng.standard_normal(n).astype(np.float32)
    a = float(np.exp(-2.0 * np.pi * lowpass_hz / sample_rate))
    # 1次IIR: y[n] = a*y[n-1] + (1-a)*x[n]。scipy を持ち込まず numpy だけで済ませる。
    out = np.empty(n, dtype=np.float32)
    acc = 0.0
    for i in range(n):
        acc = a * acc + (1.0 - a) * float(white[i])
        out[i] = acc
    rms = float(np.sqrt((out**2).mean()))
    return out / rms if rms > 0 else out  # RMS=1 に正規化


def internal_silences(audio: np.ndarray, sample_rate: int) -> list[tuple[int, int]]:
    """セグメント内部の無音区間を [開始サンプル, 終了サンプル) で返す。"""
    frame = sample_rate * INTERNAL_SILENCE_FRAME_MS // 1000
    rms = frame_rms(audio, frame)
    if rms.size == 0:
        return []
    threshold = max(rms.max() * INTERNAL_SILENCE_REL_THRESHOLD, TRIM_ABS_THRESHOLD)
    silent = rms <= threshold
    min_frames = INTERNAL_SILENCE_MIN_MS // INTERNAL_SILENCE_FRAME_MS
    spans: list[tuple[int, int]] = []
    i = 0
    while i < silent.size:
        if not silent[i]:
            i += 1
            continue
        j = i
        while j < silent.size and silent[j]:
            j += 1
        if j - i >= min_frames:
            spans.append((i * frame, j * frame))
        i = j
    return spans


def assemble(clip: dict, segments_dir: Path, sample_rate: int) -> tuple[np.ndarray, list[dict]]:
    """セグメントWAVを、指定どおりの長さの無音を挟んで連結する。"""
    parts: list[np.ndarray] = []
    gaps: list[dict] = []
    cursor = 0  # サンプル数

    for i, seg in enumerate(clip["segments"]):
        wav_path = segments_dir / f"{clip['id']}__{i:02d}.wav"
        if not wav_path.exists():
            raise SystemExit(
                f"セグメント音声が無い: {wav_path}（make_fixture_audio.ps1 を先に実行）"
            )
        audio = trim_silence(read_wav(wav_path), sample_rate)
        # SAPI が読点に入れた無音。台本には無いが実際に鳴っているので正解に載せる。
        # 文の途中なので切ってはいけない = natural_break は必ず false。
        for start, end in internal_silences(audio, sample_rate):
            gaps.append(
                {
                    "after_segment": i,
                    "origin": "tts",
                    "start_s": round((cursor + start) / sample_rate, 3),
                    "end_s": round((cursor + end) / sample_rate, 3),
                    "pause_ms": (end - start) * 1000 // sample_rate,
                    "natural_break": False,
                }
            )
        parts.append(audio)
        cursor += audio.size

        pause_ms = int(seg.get("pause_ms", 0))
        if pause_ms <= 0:
            continue
        n_silence = sample_rate * pause_ms // 1000
        gaps.append(
            {
                "after_segment": i,
                "origin": "authored",
                "start_s": round(cursor / sample_rate, 3),
                "end_s": round((cursor + n_silence) / sample_rate, 3),
                "pause_ms": pause_ms,
                "natural_break": bool(seg.get("natural_break", False)),
            }
        )
        parts.append(np.zeros(n_silence, dtype=np.float32))
        cursor += n_silence

    return np.concatenate(parts), gaps


def apply_noise(
    audio: np.ndarray, snr_db: float, cfg: dict, sample_rate: int, seed_salt: int
) -> np.ndarray:
    rng = np.random.default_rng(int(cfg.get("seed", 0)) + seed_salt)
    noise = steady_noise(audio.size, sample_rate, float(cfg.get("lowpass_hz", 450)), rng)
    target_rms = active_rms(audio, sample_rate) / (10.0 ** (snr_db / 20.0))
    return audio + noise * target_rms


def base_entries(sample_rate: int) -> list[dict]:
    """既存10文（tests/fixtures/ja/）も index に載せる。ベンチの入口を1つに保つため。"""
    sentences = [
        s.strip() for s in SENTENCES_FILE.read_text(encoding="utf-8").splitlines() if s.strip()
    ]
    entries = []
    for i, text in enumerate(sentences, start=1):
        path = BASE_DIR / f"{i:02d}.wav"
        if not path.exists():
            continue
        audio = read_wav(path)
        entries.append(
            {
                "id": f"base-{i:02d}",
                "category": "base",
                "path": path.relative_to(ROOT).as_posix(),
                "text": text,
                "duration_s": round(audio.size / sample_rate, 3),
                # 既存10文は CER/RTF 用。10文目のように文中に句点を含むものがあり、
                # どの無音が本当の文境界かを音声だけからは決められないので、
                # 区切りの採点対象からは外す（拡張コーパス側が区切りを担当する）。
                "boundary_annotated": False,
                "gaps": [],
                "expected_turns": None,
                "note": "既存のベースライン音源（変更しない）",
            }
        )
    return entries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segments-dir", required=True, type=Path)
    parser.add_argument("--out-dir", default=OUT_DIR, type=Path)
    args = parser.parse_args()

    corpus = json.loads(CORPUS_FILE.read_text(encoding="utf-8"))
    sample_rate = int(corpus["sample_rate"])
    noise_cfg = corpus.get("noise", {})
    args.out_dir.mkdir(parents=True, exist_ok=True)

    entries = base_entries(sample_rate)
    by_id = {e["id"]: e for e in entries}
    built: dict[str, np.ndarray] = {}

    # 雑音クリップは他クリップを source にするので、定義順ではなく2周に分ける。
    speech = [c for c in corpus["clips"] if c["category"] not in ("noise", "noise_only")]
    derived = [c for c in corpus["clips"] if c["category"] in ("noise", "noise_only")]

    for clip in speech:
        audio, gaps = assemble(clip, args.segments_dir, sample_rate)
        out = args.out_dir / f"{clip['id']}.wav"
        write_wav(out, audio, sample_rate)
        built[clip["id"]] = audio
        entry = {
            "id": clip["id"],
            "category": clip["category"],
            "path": out.relative_to(ROOT).as_posix(),
            "text": "".join(s["text"] for s in clip["segments"]),
            "duration_s": round(audio.size / sample_rate, 3),
            "rate": clip.get("rate", 0),
            "boundary_annotated": True,
            "gaps": gaps,
            "expected_turns": 1 + sum(1 for g in gaps if g["natural_break"]),
            "note": clip.get("note", ""),
        }
        entries.append(entry)
        by_id[clip["id"]] = entry

    for salt, clip in enumerate(derived):
        out = args.out_dir / f"{clip['id']}.wav"
        if clip["category"] == "noise_only":
            n = int(float(clip["duration_s"]) * sample_rate)
            rng = np.random.default_rng(int(noise_cfg.get("seed", 0)) + 900 + salt)
            level = 10.0 ** (float(clip.get("level_dbfs", -38)) / 20.0)
            audio = (
                steady_noise(n, sample_rate, float(noise_cfg.get("lowpass_hz", 450)), rng) * level
            )
            write_wav(out, audio, sample_rate)
            entries.append(
                {
                    "id": clip["id"],
                    "category": clip["category"],
                    "path": out.relative_to(ROOT).as_posix(),
                    "text": "",
                    "duration_s": round(audio.size / sample_rate, 3),
                    "boundary_annotated": True,
                    "gaps": [],
                    "expected_turns": 0,
                    "note": clip.get("note", ""),
                }
            )
            continue

        src_id = clip["source"]
        src = by_id.get(src_id)
        if src is None:
            raise SystemExit(f"{clip['id']}: source が見つからない: {src_id}")
        src_audio = built.get(src_id)
        if src_audio is None:
            src_audio = read_wav(ROOT / src["path"])
        audio = apply_noise(src_audio, float(clip["snr_db"]), noise_cfg, sample_rate, salt)
        write_wav(out, audio, sample_rate)
        entries.append(
            {
                "id": clip["id"],
                "category": clip["category"],
                "path": out.relative_to(ROOT).as_posix(),
                "text": src["text"],
                "duration_s": round(audio.size / sample_rate, 3),
                "source": src_id,
                "snr_db": clip["snr_db"],
                "boundary_annotated": src.get("boundary_annotated", False),
                "gaps": src["gaps"],
                "expected_turns": src["expected_turns"],
                "note": clip.get("note", ""),
            }
        )

    index = {
        "version": corpus["version"],
        "sample_rate": sample_rate,
        "_readme": (
            "生成物。ベンチ/CER ツールはここを読む。text が CER の正解、"
            "gaps[].natural_break が false のギャップで分割されたら『不自然な分割』。"
            "定義は tests/fixtures/ja_corpus.json、再生成は scripts/make_fixture_audio.ps1。"
        ),
        "clips": entries,
    }
    (args.out_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    total = sum(e["duration_s"] for e in entries)
    print(f"corpus: {len(entries)} clips / {total:.1f}s -> {args.out_dir}")


if __name__ == "__main__":
    main()
