"""性能受け入れ試験（イシュー#17 / plan.md §9 受け入れ基準）。

録音済みの授業音声（既定45分）を実時間でリプレイする先生クライアントと、
擬似生徒10接続（英語5・中国語5）を実エンジン構成のサーバーに対して走らせ、
発話終了→caption の遅延分布・サーバー常駐メモリ推移・クラッシュ/切断復元失敗を
計測し、PRD の N-01/N-05/N-08 に照らして合否判定する。1コマンドで実行できる。

    python scripts/replay_client.py                    # 既定エンジンで45分試験→否なら他エンジンで再判定
    python scripts/replay_client.py --minutes 2        # 短縮（スモーク用）
    python scripts/replay_client.py --audio rec.wav    # 実録音（16kHz mono WAV）をリプレイ
    python scripts/replay_client.py --engine nllb      # エンジン固定
    python scripts/replay_client.py --no-config-update # 合格構成をconfig.yamlへ反映しない

音源を --audio で与えない場合は tests/fixtures/ja/*.wav を無音を挟んでループ合成する
（速度・安定性の計測用。実授業の認識精度検証は実地検証#19）。レポートは docs/accept/。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.acceptance import (  # noqa: E402
    Verdict,
    judge,
    latency_stats,
    memory_trend,
)
from server.config import load_config  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "ja"
SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # 100ms @16kHz PCM16
GAP_S = 1.5  # 合成音源で発話間に挟む無音（VADの発話終了を誘発）
RSS_INTERVAL_S = 2.0
ACCEPT_PORT = 8100  # 別プロジェクトが使う8000を避ける


# ---- 音源 ----


def read_wav_16k_mono(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1:
            sys.exit(f"{path.name}: 16kHz mono ではない（{w.getframerate()}Hz/{w.getnchannels()}ch）")
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def build_stream(minutes: float, audio_path: str | None) -> np.ndarray:
    if audio_path:
        return read_wav_16k_mono(Path(audio_path))
    fixtures = sorted(FIXTURE_DIR.glob("*.wav"))
    if not fixtures:
        sys.exit(f"fixture が無い: {FIXTURE_DIR}。scripts/make_fixture_audio.ps1 を実行のこと")
    gap = np.zeros(int(SAMPLE_RATE * GAP_S), dtype=np.int16)
    target = int(minutes * 60 * SAMPLE_RATE)
    parts: list[np.ndarray] = []
    total = 0
    i = 0
    while total < target:
        utt = read_wav_16k_mono(fixtures[i % len(fixtures)])
        parts.extend((utt, gap))
        total += utt.size + gap.size
        i += 1
    return np.concatenate(parts)[:target]


# ---- 計測状態 ----


@dataclass
class Results:
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    captions: dict[tuple[int, str], int] = field(default_factory=dict)  # (seq,lang)->delay_ms
    rss_mb: list[float] = field(default_factory=list)
    disconnects: int = 0
    reconnect_failures: int = 0
    crashes: int = 0
    ran_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def record_caption(self, msg: dict) -> None:
        key = (msg["seq"], msg["lang"])
        # 同一(seq,lang)は5生徒に配信されるが遅延は同一。初回のみ採る
        self.captions.setdefault(key, msg["delay_ms"])


# ---- クライアント ----


def ws_url(port: int) -> str:
    return f"ws://127.0.0.1:{port}/ws"


# ローカル負荷試験ではクライアント側のキープアライブpingを無効化する。
# 実エンジンの重い翻訳推論が一時的にサーバーのイベントループを占有すると
# ping応答が遅れて誤切断になり得るが、それは実授業の失敗ではない（実遅延は
# delay_ms=N-01で、クラッシュは proc.poll/RSS で別途検出する）。
_WS_KW = {"ping_interval": None, "close_timeout": 5}


async def run_teacher(port: int, code: str, pcm: np.ndarray, results: Results) -> None:
    import websockets

    data = pcm.tobytes()
    try:
        async with websockets.connect(ws_url(port), max_size=None, **_WS_KW) as ws:
            await ws.send(json.dumps({"type": "join", "role": "teacher", "code": code}))
            await ws.recv()  # joined
            await ws.send(json.dumps({"type": "control", "action": "start"}))
            loop = asyncio.get_running_loop()
            start = loop.time()
            for i in range(0, len(data), CHUNK_BYTES):
                await ws.send(data[i : i + CHUNK_BYTES])
                # 実時間ペース（絶対スケジュールでドリフトを避ける）
                target = start + (i // CHUNK_BYTES + 1) * 0.1
                delay = target - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
            await ws.send(json.dumps({"type": "control", "action": "end"}))  # 最後の発話を確定
    except Exception as exc:  # 想定外の切断＝サーバー側の異常。試験は続行し記録する
        results.errors.append(f"teacher切断: {type(exc).__name__}")


async def run_student(port: int, code: str, lang: str, results: Results) -> None:
    import websockets

    last_seq = 0
    backoff = 1.0
    while not results.stop.is_set():
        try:
            async with websockets.connect(ws_url(port), **_WS_KW) as ws:
                join = {"type": "join", "role": "student", "code": code, "lang": lang}
                if last_seq:
                    join["last_seq"] = last_seq
                await ws.send(json.dumps(join))
                backoff = 1.0  # 接続成功でバックオフ回復
                async for raw in ws:
                    if results.stop.is_set():
                        return
                    msg = json.loads(raw)
                    if msg["type"] == "caption":
                        results.record_caption(msg)
                        last_seq = max(last_seq, msg["seq"])
                    elif msg["type"] == "join_rejected":
                        results.errors.append(f"student {lang}: join_rejected {msg['reason']}")
                        return
        except Exception:
            if results.stop.is_set():
                return
            # 想定外の切断。再接続を試みる（N-08 の切断復元）
            results.disconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)
            try:
                import websockets as _ws

                async with _ws.connect(ws_url(port), **_WS_KW):
                    pass  # 疎通確認のみ（本接続は次ループ）
            except Exception:
                results.reconnect_failures += 1


def tree_rss_mb(root: psutil.Process) -> float | None:
    """プロセスツリー全体（root＋子孫）の常駐メモリ合計MB。rootが消えていれば None。

    venv の python ランチャや実行環境によっては `-m server.main` の実体が
    子プロセスになるため、ツリー合計で測る（親stubだけ測ると過小評価する）。
    """
    try:
        procs = [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return None
    total = 0
    for p in procs:
        try:
            total += p.memory_info().rss
        except psutil.NoSuchProcess:
            pass
    return total / 1e6


async def sample_rss(proc: subprocess.Popen, results: Results) -> None:
    root = psutil.Process(proc.pid)
    seen_high = False
    while not results.stop.is_set():
        if proc.poll() is not None:  # サーバープロセスが終了＝クラッシュ（N-08）
            results.crashes += 1
            results.stop.set()
            return
        rss = tree_rss_mb(root)
        if rss is None:
            results.crashes += 1
            results.stop.set()
            return
        # 実サーバーは起動後ずっと数百MB以上。高値を見た後に激減＝実体プロセス消失
        if rss > 500:
            seen_high = True
        elif seen_high and rss < 50:
            results.crashes += 1
            results.stop.set()
            return
        results.rss_mb.append(rss)
        await asyncio.sleep(RSS_INTERVAL_S)


async def _drive(
    port: int, code: str, pcm: np.ndarray, n_students: int, drain_s: float, proc: subprocess.Popen
) -> Results:
    results = Results()
    langs = ["en", "zh"]
    students = [
        asyncio.create_task(run_student(port, code, langs[i % 2], results))
        for i in range(n_students)
    ]
    sampler = asyncio.create_task(sample_rss(proc, results))
    await asyncio.sleep(1.0)  # 生徒の join を先に成立させる
    started = time.monotonic()
    await run_teacher(port, code, pcm, results)
    await asyncio.sleep(drain_s)  # 最後のcaptionが届くのを待つ
    results.ran_seconds = time.monotonic() - started
    results.stop.set()
    for task in (*students, sampler):
        task.cancel()
    await asyncio.gather(*students, sampler, return_exceptions=True)
    return results


# ---- サーバー起動 ----


def write_config(engine: str, port: int, dest: Path) -> Path:
    base = load_config(ROOT / "config.yaml").model_dump()
    base["server"]["http_port"] = port
    base["mt"]["engine"] = engine
    cfg = dest / f"accept-{engine}.yaml"
    import yaml

    cfg.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
    return cfg


def wait_healthz(port: int, proc: subprocess.Popen, timeout_s: float = 300) -> None:
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/healthz"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"サーバーが起動前に終了しました (exit {proc.returncode})")
        try:
            with urllib.request.urlopen(url, timeout=3) as res:
                if res.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1.0)
    raise TimeoutError(f"サーバーが {timeout_s}s 以内に ready になりませんでした")


def fetch_code(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/teacher-info", timeout=5) as res:
        return str(json.load(res)["code"])


def _terminate_tree(proc: subprocess.Popen) -> None:
    """サーバーのプロセスツリー全体を終了する（実体が子プロセスの場合の取りこぼし防止）。"""
    try:
        root = psutil.Process(proc.pid)
        procs = [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return
    for p in procs:
        with contextlib.suppress(psutil.NoSuchProcess):
            p.terminate()
    _, alive = psutil.wait_procs(procs, timeout=10)
    for p in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            p.kill()


def run_engine_test(engine: str, args: argparse.Namespace, scratch: Path) -> dict:
    cfg = write_config(engine, args.port, scratch)
    log = open(scratch / f"server-{engine}.log", "w", encoding="utf-8")
    print(f"\n=== エンジン {engine}: サーバー起動 ===", flush=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "server.main", "--config", str(cfg)],
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_healthz(args.port, proc)
        code = fetch_code(args.port)
        pcm = build_stream(args.minutes, args.audio)
        print(f"    参加コード {code} / 音源 {pcm.size / SAMPLE_RATE:.0f}s / 生徒{args.students}名", flush=True)
        results = asyncio.run(
            _drive(args.port, code, pcm, args.students, args.drain_seconds, proc)
        )
    finally:
        _terminate_tree(proc)
        log.close()

    lat = latency_stats(list(results.captions.values()))
    mem = memory_trend(results.rss_mb)
    verdict = judge(
        lat,
        mem,
        crashes=results.crashes,
        reconnect_failures=results.reconnect_failures,
        ran_seconds=results.ran_seconds,
        target_seconds=args.minutes * 60,
    )
    return {
        "engine": engine,
        "minutes": args.minutes,
        "students": args.students,
        "captions": len(results.captions),
        "latency": lat.__dict__ if lat else None,
        "memory": mem.__dict__ if mem else None,
        "disconnects": results.disconnects,
        "reconnect_failures": results.reconnect_failures,
        "crashes": results.crashes,
        "errors": results.errors,
        "ran_seconds": round(results.ran_seconds, 1),
        "verdict": {"passed": verdict.passed, "reasons": verdict.reasons},
    }


# ---- レポート ----


def build_report(results: list[dict], system: dict) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# 性能受け入れ試験レポート（イシュー#17） — {system['date']}")
    add("")
    add(f"- 計測機: **{system['cpu']}**（{system['cores']}C/{system['threads']}T, RAM {system['ram_gb']}GB, {system['os']}）")
    add(f"- 試験長: {results[0]['minutes']}分 / 擬似生徒 {results[0]['students']}名（en/zh 半々）")
    add("- 遅延指標は caption の `delay_ms`（発話終了→送出）。ローカル同居クライアントのため受信までの差は無視できる")
    add("")
    add("> **注意**: 授業投入の正式判定は**実機（学校のi5）で45分の実授業録音**を `--audio` に与えて")
    add("> 再実行すること。合成音源（fixtureループ）は速度・安定性の確認用で、認識精度は実地検証#19で見る。")
    add("")
    add("## 受け入れ基準（PRD）")
    add("")
    add("- N-01: 発話終了→表示 中央値 ≤ 5s / 最大 ≤ 8s")
    add("- N-05: サーバー常駐メモリ ≤ 5GB")
    add("- N-08: 試験長を通してクラッシュ・切断復元失敗・メモリ増加傾向なし")
    add("")
    add("## 結果")
    add("")
    add("| エンジン | 判定 | caption数 | 遅延中央値 | 遅延p95 | 遅延最大 | ピークRSS | メモリ増分 | 切断 | 復元失敗 | クラッシュ |")
    add("|---------|------|----------|----------|--------|--------|----------|----------|------|---------|----------|")
    for r in results:
        lat = r["latency"]
        mem = r["memory"]
        verdict = "✅ 合格" if r["verdict"]["passed"] else "❌ 不合格"
        add(
            f"| {r['engine']} | {verdict} | {r['captions']} | "
            f"{lat['median_s'] if lat else '-'}s | {lat['p95_s'] if lat else '-'}s | {lat['max_s'] if lat else '-'}s | "
            f"{mem['peak_mb'] if mem else '-'}MB | {('+' + str(mem['increase_mb']) + 'MB') if mem else '-'} | "
            f"{r['disconnects']} | {r['reconnect_failures']} | {r['crashes']} |"
        )
    add("")
    for r in results:
        if not r["verdict"]["passed"]:
            add(f"### {r['engine']} の不合格理由")
            add("")
            for reason in r["verdict"]["reasons"]:
                add(f"- {reason}")
            add("")
    add("## 結論")
    add("")
    passed = [r for r in results if r["verdict"]["passed"]]
    if passed:
        chosen = passed[0]["engine"]
        add(f"- **合格構成: 翻訳エンジン = {chosen}**（config.yaml の既定に反映）")
        if len(results) > 1:
            add(f"- 既定エンジン {results[0]['engine']} が基準を満たさなかったため {chosen} で再判定し合格")
    else:
        add("- **どのエンジン構成も基準を満たさなかった** → plan.md R-01 に従い構成再検討")
        add("  （ASRを whisper small のまま beam/圧縮を見直す、より軽量なMTを追加調査、等）")
    add("")
    return "\n".join(lines)


def system_info() -> dict:
    cpu = "unknown"
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
            capture_output=True, text=True, timeout=15,
        )
        if out.stdout.strip():
            cpu = out.stdout.strip()
    except Exception:
        pass
    return {
        "cpu": cpu,
        "cores": psutil.cpu_count(logical=False),
        "threads": psutil.cpu_count(logical=True),
        "ram_gb": round(psutil.virtual_memory().total / 1e9, 1),
        "os": f"Windows",
        "date": datetime.date.today().isoformat(),
    }


def update_config_default(engine: str) -> None:
    """合格したエンジンを config.yaml の既定 mt.engine に反映する。"""
    import re

    path = ROOT / "config.yaml"
    text = path.read_text(encoding="utf-8")
    new = re.sub(
        r'(?m)^(\s*engine:\s*)("?)(hy-mt2|nllb)("?)(\s*(?:#.*)?)$',
        lambda m: f"{m.group(1)}{engine}{m.group(5)}",
        text,
        count=1,
    )
    if new != text:
        path.write_text(new, encoding="utf-8")
        print(f"config.yaml の mt.engine を {engine} に更新しました。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=45, help="試験長（分・既定45）")
    parser.add_argument("--students", type=int, default=10, help="擬似生徒数（既定10）")
    parser.add_argument("--audio", default=None, help="リプレイする録音WAV（16kHz mono）")
    parser.add_argument("--engine", default=None, choices=["hy-mt2", "nllb"], help="エンジン固定")
    parser.add_argument("--port", type=int, default=ACCEPT_PORT)
    parser.add_argument("--drain-seconds", type=float, default=20)
    parser.add_argument("--out-dir", default=str(ROOT / "docs" / "accept"))
    parser.add_argument("--no-config-update", action="store_true", help="合格構成をconfigへ反映しない")
    args = parser.parse_args()

    default_engine = load_config(ROOT / "config.yaml").mt.engine
    if args.engine:
        engines = [args.engine]
    else:
        # 既定→（否なら）もう一方、の順で試す
        other = "nllb" if default_engine == "hy-mt2" else "hy-mt2"
        engines = [default_engine, other]

    scratch = Path(args.out_dir) / "_work"
    scratch.mkdir(parents=True, exist_ok=True)
    system = system_info()

    results: list[dict] = []
    for engine in engines:
        r = run_engine_test(engine, args, scratch)
        results.append(r)
        print(f"    {engine}: {'合格' if r['verdict']['passed'] else '不合格 ' + '; '.join(r['verdict']['reasons'])}", flush=True)
        if r["verdict"]["passed"]:
            break  # 合格したら他エンジンは試さない

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{system['date']}-acceptance"
    (out_dir / f"{stem}.json").write_text(
        json.dumps({"system": system, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / f"{stem}.md").write_text(build_report(results, system), encoding="utf-8")
    print(f"\nレポート: {out_dir / (stem + '.md')}")

    passed = next((r for r in results if r["verdict"]["passed"]), None)
    if passed and not args.no_config_update and passed["engine"] != default_engine:
        update_config_default(passed["engine"])

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
