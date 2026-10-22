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
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import load_config  # noqa: E402
from scripts.replay_client import (  # noqa: E402
    SAMPLE_RATE,
    Results,
    _drive,
    _terminate_tree,
    build_stream,
    fetch_code,
    system_info,
    wait_ready,
    write_config,
)

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
) -> dict:
    """1点（言語数 × 生徒数）を測る。サーバーは点ごとに立て直す。

    立て直す理由は計数器が**通算値**だからで、差分で取ることもできるが、
    翻訳キャッシュが前の点の訳文を持ち越すと `mt_calls` が減って
    「生徒が増えたら翻訳が減った」という読めない表になる。
    """
    cfg = write_config(mt_engine, port, scratch, langs=langs)
    asr_engine = load_config(cfg).asr.engine
    log_path = scratch / f"server-{len(langs)}lang-{students}s-{mt_engine}.log"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "server.main", "--config", str(cfg)],
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
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
    return {
        "langs": langs,
        "lang_count": len(langs),
        # **実効言語数**。生徒は言語へ順に割り当てられるので、生徒数が言語数より
        # 少ない点では選択者のいない言語が出る。翻訳は選択者のいる言語にしか
        # 走らないため、MT回数を読むときの分母はこちらであって lang_count ではない
        "active_langs": min(students, len(langs)),
        "students": students,
        "mt_engine": mt_engine,
        # ASR は上書きしていない＝config.yaml の値がそのまま効く。レポートに
        # 固定文言で書くと config を変えた瞬間に嘘になるので、実際の値を残す
        "asr_engine": asr_engine,
        "asr_calls": last.get("asr_calls", 0),
        "mt_calls": last.get("mt_calls", 0),
        "mt_cache_hits": last.get("mt_cache_hits", 0),
        "captions": len(results.captions),
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
        "mt_engine": mt_engine,
        "asr_engine": None,
        "active_langs": min(students, len(langs)),
        "failed": error,
        "asr_calls": None,
        "mt_calls": None,
        "mt_cache_hits": None,
        "captions": 0,
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

    student_counts = [int(s) for s in args.students.split(",")]
    lang_counts = [int(c) for c in args.lang_counts.split(",")]
    scratch = Path(args.out_dir) / "_work"
    scratch.mkdir(parents=True, exist_ok=True)
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

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{system['date']}-scaling"
    (out_dir / f"{stem}.json").write_text(
        json.dumps(
            {"system": system, "minutes": args.minutes, "corpus": args.corpus, "points": points},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (out_dir / f"{stem}.md").write_text(
        build_report(points, system, args.minutes, args.corpus), encoding="utf-8"
    )
    print(f"\nレポート: {out_dir / (stem + '.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
