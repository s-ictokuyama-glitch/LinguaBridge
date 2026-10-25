"""スケーリングベンチ（イシュー#32）: 生徒人数と対象言語数で推論回数がどう動くか。

主張は2つで、どちらも**回数**の話である:

- **ASR回数は生徒人数で増えない**（音声は先生の1本しかないので当然のはずだが、
  「当然」を実エンジン・実WebSocket・実負荷で数値にしたことが無かった）
- **Hy-MT2 の回数は同一言語の人数で増えない**（同じ言語の生徒は同じ訳文を共有する）

#23 の不変条件テストが fake エンジン＋TestClient で同じ主張を保証しているが、
あれは「壊れたら赤くなる網」であって、実負荷での数値ではない。ここは数値を出す。

    python scripts/scale_bench.py                       # 既定の行列
    python scripts/scale_bench.py --minutes 1           # 1周を短く
    python scripts/scale_bench.py --students 1,10       # 軸を絞る

**言語軸の制約**: 実エンジン（Hy-MT2 / NLLB）が対応するのは現在 `en` / `zh` の
2言語だけで（`server/mt/hymt_engine.py` の `HYMT_LANG_LABELS`）、3言語・5言語は
翻訳できない。多言語対応はイシュー#5 の未着手スコープなので、ここでは
**言語3・5の点だけ MT を `fake` に落として**回数の主張を確かめ、
言語1・2の点は実エンジンで性能ごと測る。ASR は全点で実エンジン（sherpa）のまま。
どの点がどの構成で走ったかはレポートの `MT` 列に出る。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(os.path.abspath(Path(__file__).parent.parent))


def _scale_real_directory_chain(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    components = [Path(absolute.parts[0])]
    for part in absolute.parts[1:]:
        components.append(components[-1] / part)

    def snapshot() -> tuple[tuple[str, int, int], ...] | None:
        result: list[tuple[str, int, int]] = []
        try:
            for component in components:
                state = component.lstat()
                if (
                    not stat.S_ISDIR(state.st_mode)
                    or stat.S_ISLNK(state.st_mode)
                    or bool(
                        getattr(state, "st_file_attributes", 0)
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    )
                    or bool(
                        getattr(os.path, "isjunction", lambda _path: False)(
                            component
                        )
                    )
                ):
                    return None
                result.append((str(component), state.st_dev, state.st_ino))
        except OSError:
            return None
        return tuple(result)

    before = snapshot()
    return before is not None and snapshot() == before


def _scale_venv_site_packages() -> Path:
    venv_root = Path(os.path.abspath(sys.executable)).parent.parent
    candidates = (
        venv_root / "Lib" / "site-packages",
        venv_root
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages",
    )
    existing = [path for path in candidates if path.is_dir()]
    if len(existing) != 1 or not (venv_root / "pyvenv.cfg").is_file():
        raise RuntimeError("scale requires one auditable virtualenv site-packages")
    site_packages = Path(os.path.abspath(existing[0]))
    if not _scale_real_directory_chain(site_packages):
        raise RuntimeError(
            "scale site-packages and its ancestors must be real directories"
        )
    return site_packages


def _scale_isolated_base_paths() -> tuple[Path, ...]:
    base = Path(os.path.abspath(sys.base_prefix))
    if os.name != "nt":
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        return (
            base / "lib" / f"{version.replace('.', '')}.zip",
            base / "lib" / version,
            base / "lib" / version / "lib-dynload",
        )
    return (
        base / f"python{sys.version_info.major}{sys.version_info.minor}.zip",
        base / "DLLs",
        base / "Lib",
        base,
    )


def _scale_bootstrap_isolated_process() -> bool:
    qualified_flags = bool(
        sys.flags.isolated
        and sys.flags.no_site
        and sys.flags.ignore_environment
        and sys.flags.safe_path
        and sys.dont_write_bytecode
    )
    if __name__ == "__main__" and not qualified_flags:
        with tempfile.TemporaryDirectory(
            prefix="linguabridge-scale-qualified-pycache-"
        ) as directory:
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.upper().startswith("PYTHON")
            }
            command = [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-X",
                f"pycache_prefix={Path(os.path.abspath(directory))}",
                str(Path(os.path.abspath(__file__))),
                *sys.argv[1:],
            ]
            raise SystemExit(subprocess.call(command, cwd=str(ROOT), env=environment))
    if not qualified_flags:
        return False
    site_packages = _scale_venv_site_packages()
    if not _scale_real_directory_chain(ROOT):
        raise RuntimeError("scale root and its ancestors must be real directories")
    if (site_packages / "scripts").exists() or (site_packages / "server").exists():
        raise RuntimeError("site-packages shadows a repository package")
    base_paths = tuple(str(path) for path in _scale_isolated_base_paths())
    expected = (*base_paths, str(site_packages), str(ROOT))
    if tuple(sys.path) == base_paths:
        sys.path.extend((str(site_packages), str(ROOT)))
    return tuple(sys.path) == expected


_QUALIFIED_BOOTSTRAP = _scale_bootstrap_isolated_process()
_SCALE_PREIMPORT_MODULE_NAMES = frozenset(sys.modules)
_SCALE_CRITICAL_PREFIXES = (
    "scripts.acceptance",
    "scripts.performance_evidence",
    "scripts.replay_client",
    "scripts.text_metrics",
    "server",
    "websockets",
)
_PRELOADED_SCALE_CRITICAL_MODULES = frozenset(
    name
    for name in _SCALE_PREIMPORT_MODULE_NAMES
    if any(
        name == prefix or name.startswith(prefix + ".")
        for prefix in _SCALE_CRITICAL_PREFIXES
    )
)
if __name__ == "__main__" and "scripts" in _SCALE_PREIMPORT_MODULE_NAMES:
    _PRELOADED_SCALE_CRITICAL_MODULES |= {"scripts"}

import scripts.performance_evidence as performance_evidence_module  # noqa: E402


def _safe_scale_cache_prefix(value: object) -> Path | None:
    return performance_evidence_module._safe_empty_cache_prefix(value)


_IMPORT_PYCACHE_DIRECTORY: tempfile.TemporaryDirectory[str] | None = None
_IMPORT_PYCACHE_ROOT = (
    _safe_scale_cache_prefix(sys.pycache_prefix)
    if sys.dont_write_bytecode
    else None
)
if _IMPORT_PYCACHE_ROOT is None:
    _IMPORT_PYCACHE_DIRECTORY = tempfile.TemporaryDirectory(
        prefix="linguabridge-scale-import-pycache-"
    )
    _IMPORT_PYCACHE_ROOT = _safe_scale_cache_prefix(
        _IMPORT_PYCACHE_DIRECTORY.name
    )
    if _IMPORT_PYCACHE_ROOT is None:
        raise RuntimeError("scale import pycache prefix is not a fresh real directory")
    sys.pycache_prefix = str(_IMPORT_PYCACHE_ROOT)
sys.dont_write_bytecode = True

import scripts.replay_client as replay_client_module  # noqa: E402
import server.config as server_config_module  # noqa: E402
from server.config import load_config  # noqa: E402
from scripts.replay_client import (  # noqa: E402
    SAMPLE_RATE,
    Results,
    _drive,
    _terminate_tree,
    build_stream,
    continuous_delivery_summary,
    fetch_code,
    system_info,
    wait_ready,
    write_config,
)
from scripts.performance_evidence import (  # noqa: E402
    begin_performance_run,
    capture_imported_module_state,
    complete_performance_run,
    ensure_new_performance_run,
    fresh_python_child_environment,
    isolated_python_module_command,
    recheck_imported_module_state,
    recheck_performance_run_state,
    require_qualified_producer_bootstrap,
)

_PRODUCER_MODULE_STATE = capture_imported_module_state(
    sys.modules["scripts"],
    sys.modules[__name__],
    replay_client_module,
    performance_evidence_module,
    server_config_module,
)


def _assert_producer_modules_current() -> None:
    """Reject scaling evidence made by stale imported decision code."""

    if not _QUALIFIED_BOOTSTRAP or _PRELOADED_SCALE_CRITICAL_MODULES:
        raise performance_evidence_module.PerformanceEvidenceError(
            "scale evidence requires a fresh -I -S interpreter with no "
            "preloaded runtime modules"
        )
    require_qualified_producer_bootstrap()
    recheck_imported_module_state(_PRODUCER_MODULE_STATE)
    replay_client_module._assert_producer_modules_current()

BENCH_PORT = 8101  # replay_client(8100) と別。両方を並行して回せるように
# 点と点の間に置く待ち。同じポートを16回使い回すので、直前のサーバーの
# リッスンソケットが完全に閉じるのを待たないと次の bind が失敗しうる
SETTLE_S = 3.0
# 実エンジンで翻訳できる言語（Hy-MT2 / NLLB の対応言語）。これを超える点は fake MT
REAL_LANGS = ["en", "zh"]
# 3言語・5言語の点で使う言語コード。#23 の FIVE_LANGS と同じ並びに揃える
FIVE_LANGS = ["en", "zh", "pt", "vi", "ko"]


def run_point(
    *,
    langs: list[str],
    students: int,
    minutes: float,
    corpus: str,
    mt_engine: str,
    port: int,
    scratch: Path,
    drain_s: float,
    cfg: Path | None = None,
    source_manifest: Path,
) -> dict:
    """1点（言語数 × 生徒数）を測る。サーバーは点ごとに立て直す。

    立て直す理由は計数器が**通算値**だからで、差分で取ることもできるが、
    翻訳キャッシュが前の点の訳文を持ち越すと `mt_calls` が減って
    「生徒が増えたら翻訳が減った」という読めない表になる。
    """
    if cfg is None:
        cfg = write_config(mt_engine, port, scratch, langs=langs)
    asr_engine = load_config(cfg).asr.engine
    log_path = scratch / f"server-{len(langs)}lang-{students}s-{mt_engine}.log"
    log = open(log_path, "w", encoding="utf-8")
    with fresh_python_child_environment() as child_environment:
        child_command = isolated_python_module_command(
            "server.main",
            root=ROOT,
            environment=child_environment,
            arguments=("--config", str(cfg)),
        )
        recheck_performance_run_state(source_manifest)
        proc = subprocess.Popen(
            child_command,
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            env=child_environment,
        )
        try:
            wait_ready(port, proc)
            code = fetch_code(port)
            pcm = build_stream(minutes, None, corpus)
            print(
                f"  言語{len(langs)}（{'/'.join(langs)}） x 生徒{students}名 "
                f"/ MT={mt_engine} / 音源{pcm.size / SAMPLE_RATE:.0f}s",
                flush=True,
            )
            results = asyncio.run(_drive(port, code, pcm, students, drain_s, proc, langs))
        finally:
            _terminate_tree(proc)
            log.close()

    return summarize(results, langs=langs, students=students, mt_engine=mt_engine,
                     asr_engine=asr_engine)


def summarize(
    results: Results,
    *,
    langs: list[str],
    students: int,
    mt_engine: str,
    asr_engine: str | None,
) -> dict:
    """1点の計測結果を表の1行にする。**純粋関数**（サーバーを持たない）。

    切り出してあるのは、ここが `Results` の形に依存するため。実際に
    `Results` のプロパティを1つ消したときに16点×2分＝40分ぶんの計測が
    全部この行で落ちたので、サーバーを立てずに固定できる形にしてある。
    """
    last = results.stats_samples[-1] if results.stats_samples else {}
    delays = sorted(results.captions.values())
    dropped = results.dropped_audio()
    continuous_delivery = continuous_delivery_summary(results)
    return {
        "langs": langs,
        "lang_count": len(langs),
        # **実効言語数**。生徒は言語へ順に割り当てられるので、生徒数が言語数より
        # 少ない点では選択者のいない言語が出る。翻訳は選択者のいる言語にしか
        # 走らないため、MT回数を読むときの分母はこちらであって lang_count ではない
        "active_langs": min(students, len(langs)),
        "students": students,
        "measurement_students": results.measurement_students,
        "measurement_langs": results.measurement_langs,
        "measurement_seconds": round(results.measurement_seconds, 3),
        "audio_bytes_planned": results.audio_bytes_planned,
        "audio_bytes_sent": results.audio_bytes_sent,
        "ran_seconds": round(results.ran_seconds, 3),
        "mt_engine": mt_engine,
        # ASR は上書きしていない＝config.yaml の値がそのまま効く。レポートに
        # 固定文言で書くと config を変えた瞬間に嘘になるので、実際の値を残す
        "asr_engine": asr_engine,
        "asr_calls": last.get("asr_calls", 0),
        "mt_calls": last.get("mt_calls", 0),
        "mt_cache_hits": last.get("mt_cache_hits", 0),
        "captions": len(results.captions),
        "published_seqs": len(results.published_seqs),
        "continuous_delivery_by_student": continuous_delivery,
        "median_delay_s": (round(delays[len(delays) // 2] / 1000, 2) if delays else None),
        "max_delay_s": (round(delays[-1] / 1000, 2) if delays else None),
        "tasks": last.get("tasks", 0),
        "dropped_segments": dropped.segments,
        "frames_rejected": dropped.frames_rejected,
        "disconnects": results.disconnects,
        "reconnect_failures": results.reconnect_failures,
        "crashes": results.crashes,
        "notices": dict(results.notices),
        "errors": results.errors[:10],
        "counter_snapshot_at_s": last.get("at_s"),
        "counter_snapshot": (
            {
                key: last.get(key)
                for key in (
                    "at_s", "students", "langs", "queue_depth", "audio_queue_seconds",
                    "mt_queue_depth", "asr_calls", "mt_calls", "audio_frames_rejected",
                    "asr_dropped_segments",
                )
            }
            if last
            else None
        ),
    }


def failed_point(langs: list[str], students: int, mt_engine: str, error: str) -> dict:
    """落ちた点のプレースホルダ。

    16点で40分かかるので、1点の失敗で全部を失わない。落ちた点は表で
    `-` になり、**失敗したこと自体がレポートに残る**（黙って穴が開かない）。
    """
    return {
        "langs": langs,
        "lang_count": len(langs),
        "students": students,
        "measurement_students": 0,
        "measurement_langs": {},
        "measurement_seconds": 0.0,
        "audio_bytes_planned": 0,
        "audio_bytes_sent": 0,
        "ran_seconds": 0.0,
        "mt_engine": mt_engine,
        "asr_engine": None,
        "active_langs": min(students, len(langs)),
        "failed": error,
        "asr_calls": None,
        "mt_calls": None,
        "mt_cache_hits": None,
        "captions": 0,
        "published_seqs": 0,
        "continuous_delivery_by_student": {},
        "median_delay_s": None,
        "max_delay_s": None,
        "tasks": None,
        "dropped_segments": None,
        "frames_rejected": None,
        "disconnects": None,
        "reconnect_failures": None,
        "crashes": None,
        "notices": {},
        "errors": [error],
        "counter_snapshot_at_s": None,
        "counter_snapshot": None,
    }


def _matrix(points: list[dict], key: str) -> str:
    """行=生徒数、列=言語数 の表。主張は「行方向で動かないこと」なので行を生徒数にする。"""
    lang_counts = sorted({p["lang_count"] for p in points})
    student_counts = sorted({p["students"] for p in points})
    by = {(p["students"], p["lang_count"]): p for p in points}
    head = "| 生徒＼言語 | " + " | ".join(f"{c}言語" for c in lang_counts) + " |"
    sep = "|---------|" + "|".join(["------"] * len(lang_counts)) + "|"
    rows = []
    for s in student_counts:
        cells = []
        for c in lang_counts:
            p = by.get((s, c))
            cells.append("-" if p is None or p[key] is None else str(p[key]))
        rows.append(f"| {s}名 | " + " | ".join(cells) + " |")
    return "\n".join([head, sep, *rows])


def _verdicts(points: list[dict]) -> str:
    """主張が数値で立っているかを言葉にする。合否ではなく読み下し。

    MT の主張だけは**実効言語数が揃った点どうし**でしか比べられない。
    生徒1名 × 5言語は実効1言語なので、10名（実効5言語）と並べると
    「生徒が増えたら翻訳が増えた」に見えるが、増えたのは言語の方である。
    """
    out = ["### 読み取り", ""]
    lang_counts = sorted({p["lang_count"] for p in points})
    for key, label in (("asr_calls", "ASR"), ("mt_calls", "MT")):
        for c in lang_counts:
            col = [p for p in points if p["lang_count"] == c and p[key] is not None]
            if key == "mt_calls":
                # 実効言語数が名目に届いていない点（生徒数 < 言語数）は分母が違う
                skipped = [p["students"] for p in col if p["active_langs"] != c]
                col = [p for p in col if p["active_langs"] == c]
                if skipped:
                    out.append(
                        f"- （{c}言語: 生徒 {'/'.join(str(s) for s in sorted(skipped))} 名は"
                        f"実効言語が {c} に届かないため下の比較から除外）"
                    )
            if not col:
                out.append(f"- **{label}回数・{c}言語**: 比較できる点が無い")
                continue
            values = {p["students"]: p[key] for p in col}
            uniq = set(values.values())
            if len(uniq) == 1:
                out.append(
                    f"- **{label}回数・{c}言語**: 生徒 "
                    f"{'/'.join(str(s) for s in sorted(values))} 名で "
                    f"すべて {uniq.pop()} 回 — 生徒人数に依存しない"
                )
            else:
                lo, hi = min(uniq), max(uniq)
                out.append(
                    f"- **{label}回数・{c}言語**: {lo}〜{hi} 回で揺れた"
                    f"（{', '.join(f'{s}名={values[s]}' for s in sorted(values))}）。"
                    "生徒人数ではなく、周ごとの VAD の区切り方の違いを疑うこと"
                )
    return "\n".join(out)


def build_report(points: list[dict], system: dict, minutes: float, corpus: str) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# スケーリングベンチ（イシュー#32） — {system['date']}")
    add("")
    add(
        f"- 計測機: **{system['cpu']}**（{system['cores']}C/{system['threads']}T, "
        f"RAM {system['ram_gb']}GB, {system['os']}, Python {system.get('python', '?')}）"
    )
    add(f"- 1点あたり {minutes} 分 / 音源 `{corpus}`（点ごとにサーバーを立て直す）")
    asr_engines = sorted({p["asr_engine"] for p in points if p.get("asr_engine")})
    add(f"- ASR エンジン（`config.yaml` のまま・上書きしていない）: {', '.join(asr_engines) or '不明'}")
    add("- **3言語・5言語の点は MT が `fake`**。実エンジンの対応言語が en/zh の2つだけで、")
    add("  多言語対応は未着手（イシュー#5）。言語軸で確かめられるのは**回数の主張だけ**で、")
    add("  遅延はこの2列では実エンジンの値ではない")
    add("")
    add("## 検証する主張")
    add("")
    add("1. **ASR回数は生徒人数で増えない** — 音声は先生の1本だけなので、行方向で一定のはず")
    add("2. **MT回数は同一言語の人数で増えない** — 同じ言語の生徒は訳文を共有するので、")
    add("   行方向で一定・列方向（言語数）にだけ比例するはず")
    add("")
    add("> **生徒数 < 言語数 の点は言語軸の主張には使えない**。生徒は言語へ順に割り当てられ、")
    add("> 翻訳は**選択者のいる言語にしか走らない**ので、生徒1名の行は言語をいくつ用意しても")
    add("> 実効1言語になる。明細の「実効言語」列がその点で実際に何言語が生きていたかを表す。")
    add("")
    add("## ASR呼び出し回数")
    add("")
    add(_matrix(points, "asr_calls"))
    add("")
    add("## 翻訳（MT）呼び出し回数")
    add("")
    add(_matrix(points, "mt_calls"))
    add("")
    add("## 遅延中央値（秒）")
    add("")
    add(_matrix(points, "median_delay_s"))
    add("")
    add("## 明細")
    add("")
    add(
        "| 言語数 | 実効言語 | 生徒数 | MT | ASR回数 | MT回数 | キャッシュヒット | caption | "
        "遅延中央値 | 遅延最大 | タスク | 破棄 | 切断 | 復元失敗 | クラッシュ |"
    )
    add(
        "|-------|--------|-------|----|--------|-------|--------------|---------|"
        "--------|--------|-------|-----|-----|--------|----------|"
    )
    for p in points:
        if p.get("failed"):
            add(
                f"| {p['lang_count']} | {p['active_langs']} | {p['students']} | "
                f"{p['mt_engine']} | **失敗: {p['failed']}** | | | | | | | | | | |"
            )
            continue
        add(
            f"| {p['lang_count']} | {p['active_langs']} | {p['students']} | "
            f"{p['mt_engine']} | {p['asr_calls']} | "
            f"{p['mt_calls']} | {p['mt_cache_hits']} | {p['captions']} | "
            f"{p['median_delay_s']}s | {p['max_delay_s']}s | {p['tasks']} | "
            f"{p['dropped_segments']} | {p['disconnects']} | {p['reconnect_failures']} | "
            f"{p['crashes']} |"
        )
    add("")
    add(_verdicts(points))
    add("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=2, help="1点あたりの試験長（分）")
    parser.add_argument("--students", default="1,10,20,40", help="生徒数（カンマ区切り）")
    parser.add_argument("--lang-counts", default="1,2,3,5", help="言語数（カンマ区切り）")
    parser.add_argument("--corpus", default="ja_ext", choices=["ja", "ja_ext"])
    parser.add_argument("--engine", default="hy-mt2", choices=["hy-mt2", "nllb"])
    parser.add_argument("--port", type=int, default=BENCH_PORT)
    parser.add_argument("--drain-seconds", type=float, default=20)
    parser.add_argument("--out-dir", default=str(ROOT / "docs" / "bench"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    ensure_new_performance_run(out_dir)
    student_counts = [int(s) for s in args.students.split(",")]
    lang_counts = [int(c) for c in args.lang_counts.split(",")]
    scratch = out_dir / "_work"
    scratch.mkdir(parents=True, exist_ok=True)
    effective_configs: dict[int, Path] = {}
    for count in lang_counts:
        langs = FIVE_LANGS[:count]
        mt_engine = args.engine if set(langs) <= set(REAL_LANGS) else "fake"
        effective_configs[count] = write_config(
            mt_engine, args.port, scratch, langs=langs
        )
    _assert_producer_modules_current()
    source_manifest = begin_performance_run(
        out_dir,
        kind="scale",
        arguments=args,
        effective_configs=effective_configs.values(),
        root=ROOT,
    )
    system = system_info()

    points: list[dict] = []
    for count in lang_counts:
        langs = FIVE_LANGS[:count]
        # 実エンジンが訳せない言語が混ざる点は fake MT に落とす（冒頭のドキュメント参照）
        mt_engine = args.engine if set(langs) <= set(REAL_LANGS) else "fake"
        for students in student_counts:
            try:
                points.append(
                    run_point(
                        langs=langs,
                        students=students,
                        minutes=args.minutes,
                        corpus=args.corpus,
                        mt_engine=mt_engine,
                        port=args.port,
                        scratch=scratch,
                        drain_s=args.drain_seconds,
                        cfg=effective_configs[count],
                        source_manifest=source_manifest,
                    )
                )
            except Exception as exc:  # 1点の失敗で行列全部を失わない
                traceback.print_exc()
                points.append(
                    failed_point(langs, students, mt_engine, f"{type(exc).__name__}: {exc}")
                )
                print(f"    この点は失敗しました: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(SETTLE_S)
                continue
            p = points[-1]
            print(
                f"    ASR {p['asr_calls']}回 / MT {p['mt_calls']}回 / "
                f"caption {p['captions']}件 / 遅延中央値 {p['median_delay_s']}s",
                flush=True,
            )
            time.sleep(SETTLE_S)  # 次の点が同じポートに bind できるまで待つ

    stem = f"{system['date']}-scaling"
    json_path = out_dir / f"{stem}.json"
    markdown_path = out_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(
            {"system": system, "minutes": args.minutes, "corpus": args.corpus, "points": points},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    markdown_path.write_text(
        build_report(points, system, args.minutes, args.corpus), encoding="utf-8"
    )
    _assert_producer_modules_current()
    complete_performance_run(
        source_manifest,
        primary_report=json_path,
        artifacts=[json_path, markdown_path],
        effective_configs=effective_configs.values(),
        root=ROOT,
    )
    print(f"\nレポート: {markdown_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
