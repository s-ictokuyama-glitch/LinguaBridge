"""音声の永続化を機械判定するためのファイルシステムガード（#23）。

「記録OFF = 永続化バイト数ゼロ」「記録ONでも音声は書かない」は
プライバシー要件そのもので、デバッグ用の WAV 書き出しを1行足すだけで壊れる。

判定は2段構え:
    1. ディレクトリのスナップショット差分 — 書き込み手段（open / ndarray.tofile /
       C拡張の FILE*）に依存せず「増えたファイル」を捕まえる。これが主。
    2. 音声書き出しAPIの記録 — wave.open(書き込みモード) と、音声拡張子への
       Path.write_bytes が呼ばれたこと自体を捕まえる。どこに書いたかに関わらず気づける。

`numpy.ndarray.tofile` は不変型の属性なので差し替えられない。この経路は
スナップショット差分（1）側で捕まえる。
"""

from __future__ import annotations

import contextlib
import wave
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

# 音声として永続化されうる拡張子。記録ONでもこれらが現れてはいけない
AUDIO_SUFFIXES = {".wav", ".pcm", ".raw", ".mp3", ".flac", ".ogg", ".opus", ".webm", ".m4a"}


def snapshot(root: Path) -> dict[str, int]:
    """root 配下の全ファイルを {相対パス: バイト数} で返す。"""
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)): p.stat().st_size
        for p in root.rglob("*")
        if p.is_file()
    }


def added_files(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {
        name: size
        for name, size in after.items()
        if name not in before or before[name] != size
    }


@dataclass
class AudioWriteGuard:
    """音声書き出しAPIの呼び出し記録。"""

    calls: list[str] = field(default_factory=list)

    def assert_no_audio_written(self) -> None:
        if self.calls:
            raise AssertionError(
                f"音声書き出しAPIが呼ばれました: {', '.join(sorted(set(self.calls)))}"
            )


@contextlib.contextmanager
def guard_audio_writers() -> Iterator[AudioWriteGuard]:
    guard = AudioWriteGuard()

    real_wave_open = wave.open
    real_write_bytes = Path.write_bytes

    def wave_open(f, mode=None):  # type: ignore[no-untyped-def]
        if mode is not None and "w" in mode:
            guard.calls.append(f"wave.open({f!r}, {mode!r})")
        return real_wave_open(f, mode)

    def write_bytes(self, data):  # type: ignore[no-untyped-def]
        if self.suffix.lower() in AUDIO_SUFFIXES:
            guard.calls.append(f"Path.write_bytes({str(self)!r})")
        return real_write_bytes(self, data)

    wave.open = wave_open  # type: ignore[assignment]
    Path.write_bytes = write_bytes  # type: ignore[method-assign]
    try:
        yield guard
    finally:
        wave.open = real_wave_open  # type: ignore[assignment]
        Path.write_bytes = real_write_bytes  # type: ignore[method-assign]
