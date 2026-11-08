from __future__ import annotations

import base64
import json
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import IO

import numpy as np

from server.asr.base import ASREngine, ASRResult

PROTOCOL = 4
TIMEOUT_S = 180
CHUNK_SAMPLES = 16000


def _read_message(stream: IO[bytes]) -> dict[str, object]:
    line = stream.readline()
    if not line:
        raise RuntimeError("speech.cpp worker が応答する前に終了しました")
    try:
        message = json.loads(line)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("speech.cpp worker の応答が不正な JSON です") from exc
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise RuntimeError("speech.cpp worker の応答に type がありません")
    return message


def _write_message(stream: IO[bytes], message: dict[str, object]) -> None:
    stream.write(json.dumps(message).encode("ascii") + b"\n")


class SpeechCppEngine(ASREngine):
    def __init__(
        self, binary: Path, model: Path, *, num_threads: int, keywords: str | None = None
    ) -> None:
        if not binary.is_file():
            raise FileNotFoundError(f"speech.cpp の実行ファイルがありません: {binary}")
        if model.suffix.lower() != ".gguf" or not model.is_file():
            raise FileNotFoundError(f"speech.cpp の GGUF ファイルがありません: {model}")
        self._command = [
            str(binary.resolve()), "worker", str(model.resolve()),
            "--device", "CPU",
        ]
        if num_threads:
            self._command.extend(("--threads", str(num_threads)))
        self._keywords = keywords
        self._process: subprocess.Popen[bytes] | None = None
        self._io: ThreadPoolExecutor | None = ThreadPoolExecutor(max_workers=1)

    def __enter__(self) -> SpeechCppEngine:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            process.kill()
            process.wait()
        if self._io is not None:
            self._io.shutdown()
            self._io = None
        if process is not None:
            assert process.stdin is not None and process.stdout is not None
            with suppress(BrokenPipeError):
                process.stdin.close()
            process.stdout.close()

    def _exchange(self, operation: Callable[[], dict[str, object]]) -> dict[str, object]:
        if self._io is None:
            raise RuntimeError("speech.cpp worker は終了済みです")
        try:
            return self._io.submit(operation).result(timeout=TIMEOUT_S)
        except TimeoutError as exc:
            raise RuntimeError(f"speech.cpp worker が {TIMEOUT_S} 秒以内に応答しませんでした") from exc

    def warmup(self) -> None:
        if self._process is not None:
            return
        if self._io is None:
            raise RuntimeError("speech.cpp worker は終了済みです")
        try:
            process = subprocess.Popen(self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            self._process = process
            stdout = process.stdout
            assert stdout is not None
            ready = self._exchange(lambda: _read_message(stdout))
            if ready["type"] != "ready" or ready.get("protocol") != PROTOCOL:
                raise RuntimeError(f"speech.cpp worker の起動に失敗しました: {ready}")
        except BaseException:
            self.close()
            raise

    def transcribe(self, pcm16: np.ndarray, sample_rate: int) -> ASRResult:
        if not pcm16.size:
            return ASRResult("")
        self.warmup()
        assert self._process is not None
        stdin, stdout = self._process.stdin, self._process.stdout
        assert stdin is not None and stdout is not None

        def recognize() -> dict[str, object]:
            for seq, start in enumerate(range(0, pcm16.size, CHUNK_SAMPLES)):
                pcm = pcm16[start : start + CHUNK_SAMPLES].astype("<i2", copy=False).tobytes()
                _write_message(stdin, {
                    "type": "chunk", "id": "segment", "seq": seq,
                    "pcm": base64.b64encode(pcm).decode("ascii"),
                })
            request: dict[str, object] = {
                "type": "transcribe", "id": "segment", "sample_rate": sample_rate,
            }
            if self._keywords:
                request["keywords"] = self._keywords
            _write_message(stdin, request)
            stdin.flush()
            while True:
                reply = _read_message(stdout)
                if reply.get("id") != "segment" or reply["type"] != "progress":
                    return reply

        try:
            reply = self._exchange(recognize)
            text = reply.get("text")
            if reply.get("id") != "segment" or reply["type"] != "end" or not isinstance(text, str):
                raise RuntimeError(f"speech.cpp worker の書き起こしに失敗しました: {reply}")
        except BaseException:
            self.close()
            raise
        return ASRResult(text)
