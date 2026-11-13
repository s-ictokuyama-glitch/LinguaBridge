"""Check that a synchronization marker never exposes a partial PID."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
from threading import Event
from unittest.mock import patch

import pytest

from scripts.formal.inference_startup import block
from scripts.formal.lesson_endurance import block_operation


@pytest.mark.parametrize("publish,marker,release", [
    (lambda directory: block(directory, "startup"), "startup", "release-startup"),
    (lambda directory: block_operation(directory, "asr", 1), "entered-asr-1", "release-asr-1"),
])
def test_pid_is_complete_when_marker_becomes_visible(tmp_path, publish, marker, release):
    opened = Event()
    finish_write = Event()
    target = tmp_path / marker
    (tmp_path / release).touch()

    def held_write(path, text, *, encoding):
        with path.open("w", encoding=encoding) as stream:
            opened.set()
            if not finish_write.wait(5):
                raise TimeoutError("PID publication test did not release the writer")
            return stream.write(text)

    with patch.object(Path, "write_text", held_write), ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(publish, tmp_path)
        try:
            assert opened.wait(5), "PID writer did not create its file"
            assert not target.exists(), f"published incomplete PID: {target.read_text(encoding='ascii')!r}"
        finally:
            finish_write.set()
            writer.result(timeout=5)
    assert int(target.read_text(encoding="ascii")) == os.getpid()
