"""#45: 既定の設定＋上書き設定の二層と、現地データ領域（データルート）の解決。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from server.config import load_config

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "config.yaml"


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("override_text", [None, "", "# コメントだけ\n"])
def test_absent_or_empty_override_equals_base_only(tmp_path, override_text):
    override = tmp_path / "config.yaml"
    if override_text is not None:
        write(override, override_text)

    layered = load_config(BASE, override=override)

    assert layered == load_config(BASE)


def test_override_changes_only_its_keys_and_replaces_lists(tmp_path):
    override = write(tmp_path / "config.yaml", (
        "mt:\n"
        "  engine: nllb\n"
        "  hy_mt2: { threads: 2 }\n"
        "languages:\n"
        "  - { code: ko, label: 한국어 }\n"
    ))

    base = load_config(BASE)
    layered = load_config(BASE, override=override)

    assert layered.mt.engine == "nllb"
    assert layered.mt.hy_mt2.threads == 2
    # 上書きに無い兄弟キーは既定の設定のまま
    assert layered.mt.hy_mt2.gguf_path == base.mt.hy_mt2.gguf_path
    assert layered.mt.nllb == base.mt.nllb
    assert layered.asr == base.asr
    # リストは継ぎ足しではなく丸ごと置き換え
    assert layered.language_codes == ["ko"]
    rest = {"mt", "languages"}
    assert layered.model_dump(exclude=rest) == base.model_dump(exclude=rest)


@pytest.mark.parametrize(("keys", "value"), [
    (("turn", "strategy"), "bogus"),
    (("limits", "mt_replay_watermark"), 999),
    (("server", "advertise_ip"), "not-an-ip"),
    (("languages",), []),
])
def test_invalid_override_value_raises_the_same_validation_error(tmp_path, keys, value):
    # 同じ値を既定の設定へ直接書いたときと同じ検証エラーになる
    edited = yaml.safe_load(BASE.read_text(encoding="utf-8"))
    nested: dict = {}
    target, patch = edited, nested
    for key in keys[:-1]:
        target, patch = target[key], patch.setdefault(key, {})
    target[keys[-1]] = patch[keys[-1]] = value
    direct_file = write(tmp_path / "direct.yaml", yaml.safe_dump(edited, allow_unicode=True))
    override = write(tmp_path / "override.yaml", yaml.safe_dump(nested, allow_unicode=True))

    with pytest.raises(ValidationError) as direct:
        load_config(direct_file)
    with pytest.raises(ValidationError) as layered:
        load_config(BASE, override=override)

    assert _errors(layered.value) == _errors(direct.value)


def test_non_mapping_override_is_rejected(tmp_path):
    override = write(tmp_path / "config.yaml", "- engine: nllb\n")
    with pytest.raises(ValueError, match="上書き設定"):
        load_config(BASE, override=override)


def test_non_mapping_base_fails_validation_as_before(tmp_path):
    base = write(tmp_path / "base.yaml", "- server\n")
    override = write(tmp_path / "config.yaml", "mt: { engine: nllb }\n")
    with pytest.raises(ValidationError):
        load_config(base, override=override)


def test_data_root_resolves_certificates_and_sessions_but_not_models(tmp_path):
    base = load_config(BASE)
    data = load_config(BASE, data_root=tmp_path)

    assert data.server.cert_path() == tmp_path / "certs" / "cert.pem"
    assert data.server.key_path() == tmp_path / "certs" / "key.pem"
    assert data.recording.resolved_out_dir == tmp_path / "sessions"
    assert data.models.resolved_dir == base.models.resolved_dir
    assert data.mt.hy_mt2 == base.mt.hy_mt2


def test_data_root_leaves_absolute_output_paths_alone(tmp_path):
    certs, sessions = tmp_path / "elsewhere-certs", tmp_path / "elsewhere-sessions"
    override = write(tmp_path / "config.yaml", (
        f"server: {{ cert_dir: '{certs.as_posix()}' }}\n"
        f"recording: {{ out_dir: '{sessions.as_posix()}' }}\n"
    ))

    data = load_config(BASE, override=override, data_root=tmp_path / "data")

    assert data.server.cert_path() == certs / "cert.pem"
    assert data.recording.resolved_out_dir == sessions


def test_without_data_root_outputs_resolve_under_repository_root():
    config = load_config(BASE)

    assert config.server.cert_path() == ROOT / "certs" / "cert.pem"
    assert config.recording.resolved_out_dir == ROOT / "sessions"


def _errors(exc: ValidationError) -> list[tuple]:
    return [(e["loc"], e["type"], e["msg"]) for e in exc.errors()]
