"""実Edgeで撮った先生画面から、参加用QRを独立に復号できることを確かめる（#96）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.qa_browser import decode_qr_with_local_opencv

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "qr"


def test_decodes_join_qr_that_standard_opencv_detector_misses():
    # ポート65144の参加URLは、OpenCV標準のQRCodeDetectorが位置検出に失敗する模様になる。
    # QR自体は正しく、別方式の検出器なら読めるので、検証が製品を誤って不合格にしてはいけない
    png = (FIXTURES / "teacher-join-port-65144.png").read_bytes()
    try:
        decoded = decode_qr_with_local_opencv(png)
    except AssertionError as exc:
        pytest.skip(f"OpenCVの復号器が無い環境: {exc}")
    assert decoded["detected"], decoded
    assert decoded["value"] == "http://192.168.1.6:65144/?code=4831", decoded
