"""ASR・翻訳モデルの事前ダウンロード（イシュー#9）。

1コマンドで全モデルを取得する:

    python scripts/download_models.py                 # 全モデル
    python scripts/download_models.py --only nllb     # 個別指定（複数可）

格納先は config.yaml の models.dir（既定 %LOCALAPPDATA%/LinguaBridge/models）。
リポジトリが OneDrive 配下にあるため、GB級のモデルは同期対象外の場所に置く
（plan.md リスク R-08）。取得結果は manifest.json に記録される。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from huggingface_hub import hf_hub_download, list_repo_files, snapshot_download

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import load_config  # noqa: E402


@dataclass(frozen=True)
class ModelSpec:
    name: str
    repo_id: str
    license: str
    note: str
    kind: str  # "snapshot" | "gguf"
    subdir: str
    allow_patterns: tuple[str, ...] | None = None
    gguf_pattern: str = "*Q4_K_M*.gguf"


SPECS: list[ModelSpec] = [
    ModelSpec(
        name="kotoba",
        repo_id="kotoba-tech/kotoba-whisper-v2.0-faster",
        license="Apache-2.0",
        note="ASR既定候補（日本語特化蒸留、faster-whisper形式）",
        kind="snapshot",
        subdir="kotoba-whisper-v2.0-faster",
    ),
    ModelSpec(
        name="whisper-small",
        repo_id="Systran/faster-whisper-small",
        license="MIT",
        note="ASR切替候補（多言語small、faster-whisper形式）",
        kind="snapshot",
        subdir="faster-whisper-small",
    ),
    ModelSpec(
        name="nllb",
        repo_id="JustFrederik/nllb-200-distilled-600M-ct2-int8",
        license="CC-BY-NC-4.0",
        note="翻訳（速度重視）。非商用ライセンス — 学校の授業利用は非商用の想定（plan.md R-05/A-05）",
        kind="snapshot",
        subdir="nllb-200-distilled-600M-ct2",
    ),
    ModelSpec(
        name="nllb-tokenizer",
        repo_id="facebook/nllb-200-distilled-600M",
        license="CC-BY-NC-4.0",
        note="NLLBトークナイザ（sentencepiece）",
        kind="snapshot",
        subdir="nllb-tokenizer",
        allow_patterns=(
            "sentencepiece.bpe.model",
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
        ),
    ),
    ModelSpec(
        name="hy-mt2",
        repo_id="tencent/Hy-MT2-1.8B-GGUF",
        license="Apache-2.0",
        note="翻訳（品質重視・LLM系）。公式GGUF、llama.cpp用（Q-01の確認結果）",
        kind="gguf",
        subdir="hy-mt2",
    ),
]


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def download_one(spec: ModelSpec, dest: Path) -> Path:
    target = dest / spec.subdir
    print(f"--- {spec.name}: {spec.repo_id} -> {target}")
    if spec.kind == "snapshot":
        snapshot_download(
            repo_id=spec.repo_id,
            local_dir=target,
            allow_patterns=list(spec.allow_patterns) if spec.allow_patterns else None,
        )
        return target
    # GGUF: リポジトリから量子化パターンに合う1ファイルだけ取得
    files = list_repo_files(spec.repo_id)
    candidates = [f for f in files if fnmatch.fnmatch(f.lower(), spec.gguf_pattern.lower())]
    if not candidates:
        raise FileNotFoundError(
            f"{spec.repo_id} に {spec.gguf_pattern} に合うGGUFが見つからない。files={files}"
        )
    hf_hub_download(repo_id=spec.repo_id, filename=sorted(candidates)[0], local_dir=target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", default=None, help="格納先（既定: config.yaml の models.dir）")
    parser.add_argument(
        "--only",
        action="append",
        choices=[s.name for s in SPECS],
        help="指定モデルのみ取得（複数指定可）",
    )
    args = parser.parse_args()

    config = load_config(ROOT / "config.yaml")
    dest = Path(args.dest) if args.dest else config.models.resolved_dir
    dest.mkdir(parents=True, exist_ok=True)
    if "onedrive" in str(dest).lower():
        print(f"警告: 格納先 {dest} が OneDrive 配下に見えます（R-08）。--dest で変更を推奨。")

    manifest_path = dest / "manifest.json"
    manifest: dict = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    selected = [s for s in SPECS if not args.only or s.name in args.only]
    failures: list[str] = []
    for spec in selected:
        try:
            target = download_one(spec, dest)
        except Exception as exc:  # noqa: BLE001 - 1件の失敗で全体を止めない
            print(f"!!! {spec.name} の取得に失敗: {exc}")
            failures.append(spec.name)
            continue
        manifest[spec.name] = {
            "repo_id": spec.repo_id,
            "license": spec.license,
            "note": spec.note,
            "path": str(target),
            "bytes": dir_size(target),
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print()
    print(f"格納先: {dest}")
    for name, info in manifest.items():
        print(f"  {name:16s} {info['bytes'] / 1e9:6.2f} GB  {info['license']:14s} {info['repo_id']}")
    print()
    print("ライセンス注記:")
    print("  - NLLB-200 は CC-BY-NC 4.0（非商用限定）。学校の授業利用は非商用の想定（plan.md A-05）")
    print("  - Hy-MT2 / kotoba-whisper は Apache-2.0、whisper-small は MIT")
    if failures:
        print(f"\n失敗: {failures} — 再実行するか --only で個別に取得してください")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
