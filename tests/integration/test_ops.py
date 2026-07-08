"""運用パッケージ（イシュー#16）のサーバー側挙動テスト。

- /healthz がモデルロード完了まで 503、完了で 200（E-13）
- /api/teacher-info は HTTPS からは許可、平文HTTPの非ループバックは 403
- モデル欠損時に build_*_engine が復旧手順つきで失敗する
"""

from __future__ import annotations

import threading
import time

import pytest
from starlette.testclient import TestClient

from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, AsrConfig, ModelsConfig, MtConfig
from server.main import build_asr_engine, cert_days_remaining, create_app
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE, make_ws_test_config


def make_app(*, asr_engine=None):
    return create_app(
        make_ws_test_config(),
        asr_engine=asr_engine or FakeASREngine(),
        mt_engine=FakeTranslationEngine(["en", "zh"]),
        join_code=JOIN_CODE,
    )


class TestHealthzReadiness:
    def test_503_until_model_loaded_then_200(self):
        gate = threading.Event()  # warmup を止めてロード中を再現
        app = make_app(asr_engine=FakeASREngine(warmup_gate=gate))
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 503  # ロード中
            gate.set()
            for _ in range(200):
                if client.get("/healthz").status_code == 200:
                    break
                time.sleep(0.02)
            assert client.get("/healthz").status_code == 200


class TestTeacherInfoAccess:
    def test_https_scheme_allowed_from_non_loopback(self):
        # 先生ページは別端末のHTTPSで開くので、非ループバックでも https なら許可
        app = make_app()
        with TestClient(
            app, base_url="https://192.168.1.50", client=("192.168.1.50", 55000)
        ) as client:
            res = client.get("/api/teacher-info")
            assert res.status_code == 200
            assert res.json()["code"] == JOIN_CODE

    def test_plain_http_non_loopback_forbidden(self):
        # 平文HTTP（生徒用）の非ループバックからは参加コードを渡さない
        app = make_app()
        with TestClient(
            app, base_url="http://192.168.1.50", client=("192.168.1.50", 55000)
        ) as client:
            assert client.get("/api/teacher-info").status_code == 403


class TestModelValidation:
    def test_missing_model_reports_recovery_steps(self, tmp_path):
        config = AppConfig(
            models=ModelsConfig(dir=str(tmp_path)),
            asr=AsrConfig(engine="faster-whisper", model="faster-whisper-small"),
            mt=MtConfig(engine="fake"),
        )
        with pytest.raises(FileNotFoundError, match="download_models"):
            build_asr_engine(config)


def _write_cert(path, days_valid: int) -> None:
    """指定日数だけ有効な自己署名証明書を書き出す（E-15の残存期間チェック検証用）。"""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # not_valid_before は常に after より前（days_valid が負=期限切れでも成立させる）
        .not_valid_before(now - datetime.timedelta(days=400))
        .not_valid_after(now + datetime.timedelta(days=days_valid))
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


class TestCertExpiry:
    def test_remaining_days_for_fresh_cert(self, tmp_path):
        cert = tmp_path / "cert.pem"
        _write_cert(cert, days_valid=100)
        days = cert_days_remaining(cert)
        assert days is not None and 98 <= days <= 100

    def test_expired_cert_reports_negative(self, tmp_path):
        cert = tmp_path / "cert.pem"
        _write_cert(cert, days_valid=-5)
        days = cert_days_remaining(cert)
        assert days is not None and days < 0

    def test_missing_or_bogus_cert_returns_none(self, tmp_path):
        assert cert_days_remaining(tmp_path / "nope.pem") is None
        bogus = tmp_path / "bogus.pem"
        bogus.write_text("not a certificate")
        assert cert_days_remaining(bogus) is None
