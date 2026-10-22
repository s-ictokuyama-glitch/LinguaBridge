"""性能受け入れ試験（イシュー#17 / plan.md §9 受け入れ基準）。

録音済みの授業音声（既定45分）を実時間でリプレイする先生クライアントと、
擬似生徒10接続（英語5・中国語5）を実エンジン構成のサーバーに対して走らせ、
発話終了→caption の遅延分布・サーバー常駐メモリ推移・クラッシュ/切断復元失敗を
計測し、PRD の N-01/N-05/N-08 に照らして合否判定する。1コマンドで実行できる。

合否判定に加えて、ベースライン計測（#24）用の観測値も残す:
first caption latency / サーバーのCPU使用率 / `audio_queue_seconds`（ASR滞留の実時間）。
これらは合否には使わない（基準が無いので「今どうなっているか」の記録）。

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
import random
import statistics
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
    DRIFT_WINDOW_S,
    DroppedAudio,
    restore_gaps,
    LatencyDrift,
    ResourceTrend,
    Verdict,
    judge,
    latency_drift,
    latency_stats,
    memory_trend,
    resource_trend,
)
from server.config import load_config  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "ja"
# #22 の拡張音源（話速×3・文中の間・言い淀み・雑音SNR 20/10dB・無発話の雑音のみ）。
# 長時間試験ではこちらを使う: 同じ10文をループするより、実授業に近い揺れで
# VAD・区切り・幻覚フィルタを踏み続けられる
EXT_INDEX = ROOT / "tests" / "fixtures" / "ja_ext" / "index.json"
SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # 100ms @16kHz PCM16
GAP_S = 1.5  # 合成音源で発話間に挟む無音（VADの発話終了を誘発）
RSS_INTERVAL_S = 2.0
ACCEPT_PORT = 8100  # 別プロジェクトが使う8000を避ける


def percentile(values: list[float], pct: float) -> float:
    """パーセンタイル。scripts/acceptance.py の latency_stats と同じ数え方に揃える。"""
    ordered = sorted(values)
    idx = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return ordered[idx]


# ---- 音源 ----


def read_wav_16k_mono(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1:
            sys.exit(f"{path.name}: 16kHz mono ではない（{w.getframerate()}Hz/{w.getnchannels()}ch）")
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def corpus_clips(corpus: str) -> list[Path]:
    """ループ合成に使う wav の一覧。

    `ja` は #17 以来の既定（10文）。`ja_ext` は #22 の拡張コーパスで、
    `index.json` の並び順を守る（カテゴリが混ざる順序そのものが揺れの一部）。
    """
    if corpus == "ja":
        return sorted(FIXTURE_DIR.glob("*.wav"))
    if not EXT_INDEX.exists():
        sys.exit(f"拡張コーパスが無い: {EXT_INDEX}。scripts/build_fixture_corpus.py を実行のこと")
    index = json.loads(EXT_INDEX.read_text(encoding="utf-8"))
    return [ROOT / clip["path"] for clip in index["clips"]]


def build_stream(minutes: float, audio_path: str | None, corpus: str = "ja") -> np.ndarray:
    if audio_path:
        return read_wav_16k_mono(Path(audio_path))
    fixtures = corpus_clips(corpus)
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
    # 遅延ドリフト（#32）用の (配信開始からの経過秒, delay_ms)。captions は
    # (seq,lang) で重複排除した「分布」で、いつ届いたかを保持できない
    lat_samples: list[tuple[float, int]] = field(default_factory=list)
    rss_mb: list[float] = field(default_factory=list)
    cpu_percent: list[float] = field(default_factory=list)  # プロセスツリー合計（1コア=100%）
    # リソースリーク（#32）。スレッド・ハンドルはプロセスツリーから、
    # asyncio タスク数はサーバー自身の stats から取る（外からは見えない）
    threads: list[float] = field(default_factory=list)
    handles: list[float] = field(default_factory=list)
    tasks: list[float] = field(default_factory=list)
    # 先生が受けた過負荷通知の内訳（code -> 件数）。クールダウンで間引かれるので
    # 「起きたか」は分かるが「何件起きたか」はサーバーの計数器の側で見る
    notices: dict[str, int] = field(default_factory=dict)
    # 切断注入（#35）。再接続の差分復元が効いているかは**生徒ごとに**
    # 「自分の言語で配信された seq を全部持っているか」でしか見えない
    student_seqs: dict[str, set[int]] = field(default_factory=dict)  # sid -> 受け取った seq
    student_langs: dict[str, str] = field(default_factory=dict)  # sid -> 言語
    # サーバーが配信した seq の真値。先生の asr_final から取る（生徒側の集合だと
    # 「全員が取りこぼした seq」を配信されなかったものと区別できない）
    published_seqs: set[int] = field(default_factory=set)
    live_ws: dict[str, object] = field(default_factory=dict)  # sid -> 生きている接続
    injected_pending: set[str] = field(default_factory=set)  # 注入で閉じた直後の生徒
    injected_disconnects: int = 0
    injected_at: dict[str, float] = field(default_factory=dict)  # sid -> 注入時刻
    restore_times: list[float] = field(default_factory=list)  # 注入→復帰までの秒数
    stats_samples: list[dict] = field(default_factory=list)  # 先生が受けた stats メッセージ
    audio_started_at: float | None = None  # 先生が音声送信を始めた時刻（monotonic）
    first_caption_at: float | None = None  # 最初の caption が生徒に届いた時刻
    disconnects: int = 0
    reconnect_failures: int = 0
    # partial 字幕（#29）。turn_id -> その turn で受けた partial の ja（revision 順）
    partials: dict[int, list[str]] = field(default_factory=dict)
    first_partial_at: float | None = None  # 最初の turn.partial を受けた時刻
    first_final_at: float | None = None  # 最初の asr_final を受けた時刻
    finals: dict[int, str] = field(default_factory=dict)  # turn_id -> 確定した ja
    # turn_id -> 最初の partial / 確定 を受けた時刻。差が「先生が何秒早く読めたか」
    partial_at: dict[int, float] = field(default_factory=dict)
    final_at: dict[int, float] = field(default_factory=dict)
    crashes: int = 0
    ran_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def first_caption_s(self) -> float | None:
        """配信開始→最初の字幕。最初の発話が終わるまでの待ち時間を含む（#24）。

        delay_ms（発話終了→送出）と違い「話し始めてから画面に何か出るまで」を表す。
        生徒の体感の入り口なので、partial字幕（#29）の before 値として残す。
        """
        if self.audio_started_at is None or self.first_caption_at is None:
            return None
        return self.first_caption_at - self.audio_started_at

    @property
    def first_partial_s(self) -> float | None:
        """配信開始→最初の partial（#29）。first_caption_s と背中合わせで読む。"""
        if self.audio_started_at is None or self.first_partial_at is None:
            return None
        return self.first_partial_at - self.audio_started_at

    @property
    def first_final_s(self) -> float | None:
        """配信開始→先生の最初の確定文字起こし（#29）。

        **partial の比較対象はこれ**。生徒の first_caption_s は翻訳と配信を含むので、
        先生画面の先出し量をこれと比べると効果を過大に見積もる。
        """
        if self.audio_started_at is None or self.first_final_at is None:
            return None
        return self.first_final_at - self.audio_started_at

    def partial_summary(self) -> dict:
        """partial の実効値（#29 の判定材料）。

        - revisions_per_turn: 1 turn あたり何回先出しできたか
        - mismatch_rate: 最後の partial と確定 ja の文字距離（読み直しの量）
        - preceded_rate: 確定した turn のうち partial が先行した割合
        """
        from scripts.text_metrics import cer_counts, corpus_rate

        revisions = [len(v) for v in self.partials.values()]
        paired = [
            (self.partials[tid][-1], ja)
            for tid, ja in self.finals.items()
            if self.partials.get(tid)
        ]
        # 参照＝確定した ja、仮説＝最後の partial。短い turn が過大評価されないよう
        # クリップ単位の平均ではなく編集距離の総和で取る（#22 の text_metrics の作法）
        counts = [cer_counts(final, last) for last, final in paired]
        mismatch_rate = corpus_rate(counts)
        exact = sum(1 for c in counts if c.distance == 0)
        # 先生が何秒早く読めたか。**turn ごとに**取る（最初の1発話だけでは音源の
        # 出だしの長さに引きずられる）
        leads = [
            self.final_at[tid] - self.partial_at[tid]
            for tid in self.final_at
            if tid in self.partial_at
        ]
        return {
            "turns": len(self.finals),
            "turns_with_partial": len(paired),
            "preceded_rate": (len(paired) / len(self.finals)) if self.finals else None,
            "partials": sum(revisions),
            "revisions_per_turn": (statistics.mean(revisions) if revisions else None),
            "revisions_max": max(revisions) if revisions else None,
            "mismatch_rate": mismatch_rate,
            "exact_match_rate": (exact / len(counts)) if counts else None,
            "first_partial_s": self.first_partial_s,
            "first_final_s": self.first_final_s,
            # partial が確定より何秒早く出たか（turn ごと）。partial の価値そのもの
            "lead_median_s": (round(statistics.median(leads), 3) if leads else None),
            "lead_max_s": (round(max(leads), 3) if leads else None),
            "lead_min_s": (round(min(leads), 3) if leads else None),
        }

    def dropped_audio(self) -> DroppedAudio:
        """試験中に失われた音声（サーバーの通算計数器の最終値）。

        stats は2秒ごとに来るので最後のサンプルが通算値になる。
        1件も stats を受けていないなら「失われていない」ではなく
        「観測していない」だが、その場合は caption も 0 件で別途落ちる。
        """
        if not self.stats_samples:
            return DroppedAudio()
        last = self.stats_samples[-1]
        return DroppedAudio(
            segments=last["asr_dropped_segments"],
            seconds=last["asr_dropped_seconds"],
            frames_rejected=last["audio_frames_rejected"],
        )

    def record_caption(self, msg: dict) -> None:
        if self.first_caption_at is None and msg["delay_ms"] > 0:
            self.first_caption_at = time.monotonic()  # 復元再送(delay_ms=0)は初回に数えない
        key = (msg["seq"], msg["lang"])
        # 同一(seq,lang)は5生徒に同じ delay_ms で配信される。加えて再接続復元の
        # caption は delay_ms=0（歴史的再送、pipeline側で0固定）で届きうる。ライブ配信は
        # 復元ジョブより優先度が高く必ず先に届くため、setdefault(初回優先)で
        # ライブの実遅延を採り、後着の復元0で過小評価しない。
        if key not in self.captions and self.audio_started_at is not None and msg["delay_ms"] > 0:
            # ドリフトは「初めて見た (seq,lang)」だけで数える。5生徒に同じ caption が
            # 配られるぶんを混ぜると、生徒数が窓の重みになってしまう。
            # **復元再送（delay_ms=0）は除く**（`first_caption_at` と同じ理由）。
            # 0秒を窓に混ぜると最初の窓の中央値が 0 に落ち、相対閾値
            # （increase > first * 0.5）が常に真になってドリフト判定が壊れる
            self.lat_samples.append(
                (time.monotonic() - self.audio_started_at, msg["delay_ms"])
            )
        self.captions.setdefault(key, msg["delay_ms"])


# ---- クライアント ----


def ws_url(port: int) -> str:
    return f"ws://127.0.0.1:{port}/ws"


# ローカル負荷試験ではクライアント側のキープアライブpingを無効化する。
# 実エンジンの重い翻訳推論が一時的にサーバーのイベントループを占有すると
# ping応答が遅れて誤切断になり得るが、それは実授業の失敗ではない（実遅延は
# delay_ms=N-01で、クラッシュは proc.poll/RSS で別途検出する）。
_WS_KW = {"ping_interval": None, "close_timeout": 5}


async def drain_teacher(ws, results: Results) -> None:
    """先生に届くメッセージを読み続ける。stats（#24 の観測値）だけ記録する。

    読まないとサーバー→先生の送信がバッファに溜まり続けるので、いずれにせよ必要。
    """
    try:
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "turn.partial":
                if results.first_partial_at is None:
                    results.first_partial_at = time.monotonic()
                results.partials.setdefault(msg["turn_id"], []).append(msg["ja"])
                results.partial_at.setdefault(msg["turn_id"], time.monotonic())
            elif msg["type"] == "asr_final":
                if results.first_final_at is None:
                    results.first_final_at = time.monotonic()
                # turn_id は #29 以降のサーバーだけが持つ（既定 0 で読む）
                results.finals[msg.get("turn_id", 0)] = msg["ja"]
                results.published_seqs.add(msg["seq"])  # 復元判定の真値（#35）
                results.final_at[msg.get("turn_id", 0)] = time.monotonic()
            elif msg["type"] == "stats":
                results.stats_samples.append(
                    {
                        "at_s": round(time.monotonic() - (results.audio_started_at or 0), 1),
                        "queue_depth": msg["queue_depth"],
                        # 旧サーバーには無いフィールドなので既定 0 で読む
                        "audio_queue_seconds": msg.get("audio_queue_seconds", 0.0),
                        "median_delay_ms": msg["median_delay_ms"],
                        "mt_queue_depth": msg.get("mt_queue_depth", 0),
                        "overloaded": msg.get("overloaded", False),
                        # #26 の翻訳キャッシュ。旧サーバーには無いので既定 0
                        "mt_cache_hit_rate": msg.get("mt_cache_hit_rate", 0.0),
                        "mt_cache_hits": msg.get("mt_cache_hits", 0),
                        "mt_cache_size": msg.get("mt_cache_size", 0),
                        # 通算の推論回数と破棄（#32）。旧サーバーには無いので既定 0
                        "asr_calls": msg.get("asr_calls", 0),
                        "asr_partial_calls": msg.get("asr_partial_calls", 0),
                        "mt_calls": msg.get("mt_calls", 0),
                        "audio_frames_rejected": msg.get("audio_frames_rejected", 0),
                        "asr_dropped_segments": msg.get("asr_dropped_segments", 0),
                        "asr_dropped_seconds": msg.get("asr_dropped_seconds", 0.0),
                        "tasks": msg.get("tasks", 0),
                    }
                )
                if msg.get("tasks"):
                    results.tasks.append(float(msg["tasks"]))
            elif msg["type"] == "error":
                # 過負荷通知（audio_dropped / mt_backlog / asr_timeout / mt_timeout）と
                # 無音警告（mic_silent）。授業中に先生の画面へ出るものがそのまま来る
                code = msg.get("code", "unknown")
                results.notices[code] = results.notices.get(code, 0) + 1
    except Exception:
        pass  # 送信側の切断は run_teacher が記録する


async def run_teacher(port: int, code: str, pcm: np.ndarray, results: Results) -> None:
    import websockets

    data = pcm.tobytes()
    reader: asyncio.Task[None] | None = None
    try:
        async with websockets.connect(ws_url(port), max_size=None, **_WS_KW) as ws:
            await ws.send(json.dumps({"type": "join", "role": "teacher", "code": code}))
            await ws.recv()  # joined
            await ws.send(json.dumps({"type": "control", "action": "start"}))
            reader = asyncio.create_task(drain_teacher(ws, results))
            loop = asyncio.get_running_loop()
            start = loop.time()
            results.audio_started_at = time.monotonic()
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
    finally:
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader


async def run_student(port: int, code: str, lang: str, results: Results, sid: str) -> None:
    """擬似生徒。切断されたら last_seq で再接続して復元する（N-08）。

    disconnects: 確立済み接続が**想定外に**切れた回数。
    reconnect_failures: 切断後の再接続（本接続）に失敗した回数（＝復元失敗）。
    injected_disconnects: 試験が意図的に切った回数（#35）。**上の2つには混ぜない** —
    混ぜると「注入すればするほど不合格に近づく」という無意味な判定になる。
    """
    import websockets

    results.student_langs[sid] = lang
    results.student_seqs.setdefault(sid, set())
    last_seq = 0
    backoff = 1.0
    need_reconnect = False  # 直前に切断された＝この接続試行は「復元」
    while not results.stop.is_set():
        try:
            async with websockets.connect(ws_url(port), **_WS_KW) as ws:
                join = {"type": "join", "role": "student", "code": code, "lang": lang}
                if last_seq:
                    join["last_seq"] = last_seq
                await ws.send(json.dumps(join))
                backoff = 1.0  # 接続成功でバックオフ回復
                need_reconnect = False  # 復元成功
                results.live_ws[sid] = ws
                injected_at = results.injected_at.pop(sid, None)
                if injected_at is not None:  # 注入で切られたぶんの復帰時間（#35）
                    results.restore_times.append(time.monotonic() - injected_at)
                async for raw in ws:
                    if results.stop.is_set():
                        return
                    msg = json.loads(raw)
                    if msg["type"] == "caption":
                        results.record_caption(msg)
                        results.student_seqs[sid].add(msg["seq"])
                        last_seq = max(last_seq, msg["seq"])
                    elif msg["type"] == "join_rejected":
                        results.errors.append(f"student {lang}: join_rejected {msg['reason']}")
                        return
            # 例外なしで async-for を抜けた＝サーバーが接続を閉じた
            if results.stop.is_set():
                return
            _count_disconnect(results, sid)
            need_reconnect = True
        except Exception:
            if results.stop.is_set():
                return
            if need_reconnect:
                results.reconnect_failures += 1  # 切断後の再接続に失敗＝復元失敗
            else:
                _count_disconnect(results, sid)
                need_reconnect = True
        finally:
            results.live_ws.pop(sid, None)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 15.0)


def _count_disconnect(results: Results, sid: str) -> None:
    """切断を数える。**試験が自分で切ったぶんは想定外に数えない**（#35）。"""
    if sid in results.injected_pending:
        results.injected_pending.discard(sid)
        return
    results.disconnects += 1


async def inject_disconnects(results: Results, every_s: float, seed: int = 0) -> None:
    """N秒ごとにランダムな生徒1名の接続を切る（#35）。

    実授業で起きるのは Wi-Fi 瞬断と端末スリープで、どちらもクライアントから見れば
    「接続が閉じた」になる。close code 1001（going away）はスリープ相当。
    **生徒側の再接続実装は触らない** — 触ると「試験のために作った経路」を試すことになる。

    生徒の再接続バックオフは 1.0s 起点なので、`every_s` をそれより短くすると
    復元の失敗ではなく**試験の設計ミス**として欠落が出る。
    """
    rng = random.Random(seed)
    while not results.stop.is_set():
        await asyncio.sleep(every_s)
        if results.stop.is_set():
            return
        live = [(sid, ws) for sid, ws in results.live_ws.items() if ws is not None]
        if not live:
            continue
        sid, ws = rng.choice(live)
        results.injected_pending.add(sid)
        results.injected_at[sid] = time.monotonic()
        results.injected_disconnects += 1
        with contextlib.suppress(Exception):
            await ws.close(code=1001)  # going away（端末スリープ相当）


def proc_tree(root: psutil.Process) -> list[psutil.Process] | None:
    """root＋子孫のプロセス一覧。rootが既に消えていれば None。

    venv の python ランチャや実行環境によっては `-m server.main` の実体が
    子プロセスになるため、メモリ計測も終了もツリー全体を対象にする
    （親stubだけを見ると過小評価・取りこぼしになる）。
    """
    try:
        return [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return None


def tree_rss_mb(root: psutil.Process) -> float | None:
    """プロセスツリー全体の常駐メモリ合計MB。rootが消えていれば None。"""
    procs = proc_tree(root)
    if procs is None:
        return None
    total = 0
    for p in procs:
        try:
            total += p.memory_info().rss
        except psutil.NoSuchProcess:
            pass
    return total / 1e6


def tree_counts(root: psutil.Process) -> tuple[float, float | None] | None:
    """プロセスツリー全体の (スレッド数, ハンドル数) 合計（#32）。

    ハンドルは Windows の `num_handles()`、POSIX では `num_fds()` を使う。
    **どちらも取れない環境ではハンドルを None で返す**。0 を返すと
    レポートに `0→0` と出て「測ったが増えなかった」と誤読されるが、
    実際には測れていない（レポートは `-` になるべき）。
    """
    procs = proc_tree(root)
    if procs is None:
        return None
    threads = 0.0
    handles: float | None = None
    for p in procs:
        try:
            threads += p.num_threads()
            counter = getattr(p, "num_handles", None) or getattr(p, "num_fds", None)
            if counter is not None:
                handles = (handles or 0.0) + counter()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return threads, handles


class TreeCpuSampler:
    """プロセスツリー全体のCPU使用率（前回呼び出しからの区間平均、1コア=100%）。

    psutil の cpu_percent(interval=None) は **Process オブジェクトごとに**前回の
    CPU時間を覚えて差分を返す。毎回ツリーを取り直して新しい Process を作ると
    常に「初回」＝0.0 になるので、pid ごとに Process を使い回す。
    論理コア数×100 が上限。初回サンプルは基準作りなので None を返す。
    """

    def __init__(self, root: psutil.Process) -> None:
        self._root = root
        self._procs: dict[int, psutil.Process] = {}
        self._primed = False

    def sample(self) -> float | None:
        procs = proc_tree(self._root)
        if procs is None:
            return None
        total = 0.0
        alive: dict[int, psutil.Process] = {}
        for proc in procs:
            tracked = self._procs.get(proc.pid, proc)
            alive[proc.pid] = tracked
            try:
                total += tracked.cpu_percent(interval=None)
            except psutil.NoSuchProcess:
                pass
        self._procs = alive
        if not self._primed:
            self._primed = True
            return None  # 差分の基準を作っただけ
        return total


async def sample_rss(proc: subprocess.Popen, results: Results) -> None:
    root = psutil.Process(proc.pid)
    seen_high = False
    cpu_sampler = TreeCpuSampler(root)
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
        counts = tree_counts(root)
        if counts is not None:
            results.threads.append(counts[0])
            if counts[1] is not None:
                results.handles.append(counts[1])
        cpu = cpu_sampler.sample()
        if cpu is not None:
            results.cpu_percent.append(cpu)
        await asyncio.sleep(RSS_INTERVAL_S)


async def _drive(
    port: int,
    code: str,
    pcm: np.ndarray,
    n_students: int,
    drain_s: float,
    proc: subprocess.Popen,
    langs: list[str] | None = None,
    disconnect_every_s: float | None = None,
) -> Results:
    results = Results()
    langs = langs or ["en", "zh"]
    students = [
        asyncio.create_task(run_student(port, code, langs[i % len(langs)], results, f"s{i}"))
        for i in range(n_students)
    ]
    sampler = asyncio.create_task(sample_rss(proc, results))
    injector = (
        asyncio.create_task(inject_disconnects(results, disconnect_every_s))
        if disconnect_every_s
        else None
    )
    await asyncio.sleep(1.0)  # 生徒の join を先に成立させる
    started = time.monotonic()
    await run_teacher(port, code, pcm, results)
    if injector is not None:
        # drain 中は切らない。最後に切ったぶんの復元を drain で完了させるため
        # （切ったまま試験が終わると「復元されなかった」と読めてしまう）
        injector.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await injector
        injector = None
    await asyncio.sleep(drain_s)  # 最後のcaptionが届くのを待つ
    results.ran_seconds = time.monotonic() - started
    results.stop.set()
    extra = [t for t in (sampler, injector) if t is not None]
    for task in (*students, *extra):
        task.cancel()
    await asyncio.gather(*students, *extra, return_exceptions=True)
    return results


# ---- サーバー起動 ----


def write_config(
    engine: str,
    port: int,
    dest: Path,
    asr_engine: str | None = None,
    partial: bool | None = None,
    langs: list[str] | None = None,
    mt_cache_size: int | None = None,
) -> Path:
    """試験用の config を書き出す。

    `asr_engine` は #28 の ASR A/B 用、`partial` は #29 の partial A/B 用、
    `langs` は #32 のスケーリング試験用（いずれも None なら config.yaml のまま）。
    """
    base = load_config(ROOT / "config.yaml").model_dump()
    base["server"]["http_port"] = port
    base["mt"]["engine"] = engine
    if asr_engine:
        base["asr"]["engine"] = asr_engine
    suffix = f"{'-' + asr_engine if asr_engine else ''}"
    if partial is not None:
        base["partial"]["enabled"] = partial
        suffix += f"-partial{'on' if partial else 'off'}"
    if langs:
        # 生徒が選べる言語は config の languages が決める。ここを絞らないと
        # 生徒の join が拒否される（言語コードの検証は join の側にある）
        base["languages"] = [{"code": c, "label": c} for c in langs]
        suffix += f"-{len(langs)}lang"
    if mt_cache_size is not None:
        # 0 でキャッシュ無効＝**MT負荷の上界**を測る（#32）。ループ合成の音源は
        # 同じ原文が何周も来るのでヒット率が実授業よりはるかに高くなり、
        # 既定のままでは Hy-MT2 がほとんど動かない状態を計測してしまう
        base["mt"]["cache_size"] = mt_cache_size
        suffix += f"-cache{mt_cache_size}"
    cfg = dest / f"accept-{engine}{suffix}.yaml"
    import yaml

    cfg.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
    return cfg


def wait_ready(port: int, proc: subprocess.Popen, timeout_s: float = 300) -> None:
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/ready"  # readiness（#25 W-2）。/healthz は liveness
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
    except psutil.NoSuchProcess:
        return
    procs = proc_tree(root)
    if procs is None:
        return
    for p in procs:
        with contextlib.suppress(psutil.NoSuchProcess):
            p.terminate()
    _, alive = psutil.wait_procs(procs, timeout=10)
    for p in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            p.kill()


def run_engine_test(engine: str, args: argparse.Namespace, scratch: Path) -> dict:
    cfg = write_config(
        engine,
        args.port,
        scratch,
        getattr(args, "asr", None),
        getattr(args, "partial", None),
        getattr(args, "langs", None),
        getattr(args, "mt_cache_size", None),
    )
    log = open(scratch / f"server-{engine}.log", "w", encoding="utf-8")
    print(f"\n=== エンジン {engine}: サーバー起動 ===", flush=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "server.main", "--config", str(cfg)],
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_ready(args.port, proc)
        code = fetch_code(args.port)
        pcm = build_stream(args.minutes, args.audio, getattr(args, "corpus", "ja"))
        print(f"    参加コード {code} / 音源 {pcm.size / SAMPLE_RATE:.0f}s / 生徒{args.students}名", flush=True)
        results = asyncio.run(
            _drive(
                args.port,
                code,
                pcm,
                args.students,
                args.drain_seconds,
                proc,
                getattr(args, "langs", None),
                getattr(args, "disconnect_every_s", None),
            )
        )
    finally:
        _terminate_tree(proc)
        log.close()

    queued = [s["audio_queue_seconds"] for s in results.stats_samples]
    depths = [s["queue_depth"] for s in results.stats_samples]
    mt_depths = [float(s["mt_queue_depth"]) for s in results.stats_samples]
    dropped = results.dropped_audio()
    # 切断注入下の復元（#35）。注入していない周では student_seqs は埋まるが
    # 欠落は出ない（切れていないので当然）＝判定に影響しない
    restore = restore_gaps(
        results.student_seqs,
        results.student_langs,
        dict.fromkeys(set(results.student_langs.values()), results.published_seqs),
        history_limit=load_config(ROOT / "config.yaml").history_resend,
    )
    lat = latency_stats(list(results.captions.values()))
    mem = memory_trend(results.rss_mb)
    # 長時間試験の追加判定（#32）。短い周では窓が1つしか作れず drift は None になり、
    # そのときは判定に効かない（＝スモークを長時間試験の基準で落とさない）
    drift = latency_drift(results.lat_samples)
    resources = [
        resource_trend("threads", results.threads),
        resource_trend("handles", results.handles),
        resource_trend("tasks", results.tasks),
        # queue growth（チケットが名指しした確認項目）。最大値だけでは
        # 「一度跳ねた」と「増え続けている」が区別できないので、
        # メモリと同じ前半/後半の中央値で見る
        resource_trend("audio_queue_seconds", queued),
        resource_trend("mt_queue_depth", mt_depths),
    ]
    verdict = judge(
        lat,
        mem,
        crashes=results.crashes,
        reconnect_failures=results.reconnect_failures,
        ran_seconds=results.ran_seconds,
        target_seconds=args.minutes * 60,
        drift=drift,
        resources=resources,
        dropped=dropped,
        restore=restore,
    )
    return {
        "engine": engine,
        "minutes": args.minutes,
        "students": args.students,
        "corpus": getattr(args, "corpus", "ja"),
        "disconnect_every_s": getattr(args, "disconnect_every_s", None),
        "mt_cache_size": getattr(args, "mt_cache_size", None),
        "langs": getattr(args, "langs", None) or ["en", "zh"],
        "captions": len(results.captions),
        "latency": lat.__dict__ if lat else None,
        "memory": mem.__dict__ if mem else None,
        # 以下は #24 のベースライン用の観測値（合否判定には使わない）
        "first_caption_s": (
            round(results.first_caption_s, 2) if results.first_caption_s is not None else None
        ),
        "cpu_percent": (
            {
                "cores": psutil.cpu_count(logical=True),
                "median": round(statistics.median(results.cpu_percent), 1),
                "p95": round(percentile(results.cpu_percent, 95), 1),
                "max": round(max(results.cpu_percent), 1),
                "samples": len(results.cpu_percent),
            }
            if results.cpu_percent
            else None
        ),
        "audio_queue_seconds": (
            {
                "median": round(statistics.median(queued), 2),
                "p95": round(percentile(queued, 95), 2),
                "max": round(max(queued), 2),
                "samples": len(queued),
            }
            if queued
            else None
        ),
        "queue_depth": (
            {"median": round(statistics.median(depths), 1), "max": max(depths)}
            if depths
            else None
        ),
        "overloaded_samples": sum(1 for s in results.stats_samples if s["overloaded"]),
        # partial 字幕（#29）。partial 無効の周でも空の要約が入り、A/B が同じ形で並ぶ
        "partial": results.partial_summary(),
        # 翻訳キャッシュ（#26 B-4）。最後のサンプルが通算値になる
        "mt_cache": (
            {
                "hit_rate": results.stats_samples[-1]["mt_cache_hit_rate"],
                "hits": results.stats_samples[-1]["mt_cache_hits"],
                "size": results.stats_samples[-1]["mt_cache_size"],
            }
            if results.stats_samples
            else None
        ),
        # 連続動作の健全性（#32）
        "drift": (
            {
                "window_s": DRIFT_WINDOW_S,
                "first_median_s": drift.first_median_s,
                "last_median_s": drift.last_median_s,
                "increase_s": drift.increase_s,
                "drifting": drift.drifting,
                "empty_windows": drift.empty_windows,
                "windows": [w.__dict__ for w in drift.windows],
            }
            if drift
            else None
        ),
        "resources": [r.__dict__ for r in resources if r is not None],
        "dropped": {**dropped.__dict__, "notices": results.notices},
        "restore": (
            {
                **restore.__dict__,
                "injected_disconnects": results.injected_disconnects,
                "restore_median_s": (
                    round(statistics.median(results.restore_times), 2)
                    if results.restore_times
                    else None
                ),
                "restore_max_s": (
                    round(max(results.restore_times), 2) if results.restore_times else None
                ),
                "published_seqs": len(results.published_seqs),
            }
            if restore
            else None
        ),
        "calls": (
            {
                "asr": results.stats_samples[-1]["asr_calls"],
                "asr_partial": results.stats_samples[-1]["asr_partial_calls"],
                "mt": results.stats_samples[-1]["mt_calls"],
                "tasks": results.stats_samples[-1]["tasks"],
            }
            if results.stats_samples
            else None
        ),
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
    add(f"- 計測機: **{system['cpu']}**（{system['cores']}C/{system['threads']}T, RAM {system['ram_gb']}GB, {system['os']}, Python {system.get('python', '?')}）")
    add(
        f"- 試験長: {results[0]['minutes']}分 / 擬似生徒 {results[0]['students']}名"
        f"（{'/'.join(results[0].get('langs') or ['en', 'zh'])} を順に割当）"
    )
    add(f"- 音源: {results[0].get('corpus', 'ja')}（`--audio` 指定時は録音そのもの）")
    if results[0].get("mt_cache_size") is not None:
        add(
            f"- **翻訳キャッシュを `{results[0]['mt_cache_size']}` に上書きして計測**"
            "（0 = 無効）。ループ合成の音源はヒット率が実授業よりはるかに高くなるので、"
            "既定のままでは Hy-MT2 がほとんど動かない状態を測ることになる"
        )
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
    add("## 観測値（#24 ベースライン。合否判定には使わない）")
    add("")
    add("| エンジン | 初回字幕 | CPU中央値 | CPU最大 | ASR滞留中央値 | ASR滞留最大 | キュー深度最大 | 過負荷サンプル |")
    add("|---------|---------|----------|--------|-------------|-----------|-------------|-------------|")
    for r in results:
        cpu = r.get("cpu_percent")
        aq = r.get("audio_queue_seconds")
        qd = r.get("queue_depth")
        first = f"{r['first_caption_s']}s" if r.get("first_caption_s") is not None else "-"
        add(
            f"| {r['engine']} | {first} | "
            f"{str(cpu['median']) + '%' if cpu else '-'} | {str(cpu['max']) + '%' if cpu else '-'} | "
            f"{str(aq['median']) + 's' if aq else '-'} | {str(aq['max']) + 's' if aq else '-'} | "
            f"{qd['max'] if qd else '-'} | {r.get('overloaded_samples', '-')} |"
        )
    add("")
    add("### 連続動作の健全性（#32）")
    add("")
    add("| エンジン | 遅延ドリフト | スレッド | ハンドル | asyncioタスク | ASR滞留 | MTキュー | 破棄 | 過負荷通知 |")
    add("|---------|------------|--------|---------|-------------|--------|---------|-----|----------|")
    for r in results:
        d = r.get("drift")
        res = {x["name"]: x for x in r.get("resources", [])}

        def cell(name: str) -> str:
            x = res.get(name)
            if not x:
                return "-"
            mark = " ⚠️" if x["growing"] else ""
            return f"{x['baseline']:g}→{x['final']:g}{mark}"

        drift_cell = (
            f"{d['first_median_s']}s→{d['last_median_s']}s"
            + (" ⚠️" if d["drifting"] else "")
            if d
            else "-（窓が1つ）"
        )
        dropped = r.get("dropped", {})
        notices = dropped.get("notices") or {}
        add(
            f"| {r['engine']} | {drift_cell} | {cell('threads')} | {cell('handles')} | "
            f"{cell('tasks')} | {cell('audio_queue_seconds')} | {cell('mt_queue_depth')} | "
            f"容量{dropped.get('segments', 0)}件"
            f"（{dropped.get('seconds', 0.0):.1f}s）/ 上限超過{dropped.get('frames_rejected', 0)}枚 | "
            f"{', '.join(f'{k}×{v}' for k, v in notices.items()) if notices else 'なし'} |"
        )
    add("")
    add(f"- ドリフト = {DRIFT_WINDOW_S:.0f}秒窓ごとの遅延中央値の、最初の窓と最後の窓の比較。")
    add("  「遅いか」ではなく**「だんだん遅くなるか」**を見る（p95 では前半の速さに薄められる）")
    add("- スレッド/ハンドル/タスク/キューは前半中央値→後半中央値。⚠️ は増加傾向の判定に当たったもの")
    add("  （`-` は測れなかった指標。0→0 と区別する）")
    add("- ASR滞留・MTキューは **queue growth** の判定。最大値だけでは「一度跳ねた」と")
    add("  「増え続けている」が区別できないので、メモリと同じ前半/後半の中央値で見る")
    add("- 破棄は2種類で意味が違う。**容量** = `limits.asr_queue_seconds`(30s) 到達で捨てた")
    add("  Segment（0でなければ授業で字幕が欠けている）。**上限超過** = `limits.max_audio_bytes`")
    add("  を超えたフレーム（不正入力側で、正常なクライアントでは 0）。")
    add("  送信キュー溢れ（遅い生徒）は破棄ではなく切断なので、上の表の「切断」に出る")
    add("- `asyncio` タスク数は**生徒ごとのWSハンドラを含む**ので、接続数が動く試験では")
    add("  リークではなく接続数の増減を見ていることになる。生徒数が一定の試験でだけ読める")
    add("")
    for r in results:
        d = r.get("drift")
        if not d or not d["windows"]:
            continue
        add(f"<details><summary>{r['engine']}: 窓ごとの遅延推移</summary>")
        add("")
        add("| 窓 | 開始 | caption数 | 中央値 | 最大 |")
        add("|----|------|----------|-------|------|")
        for w in d["windows"]:
            add(
                f"| {w['index']} | {w['start_s'] / 60:.0f}分 | {w['count']} | "
                f"{w['median_s']}s | {w['max_s']}s |"
            )
        add("")
        add("</details>")
        add("")
    if any(r.get("restore") and r["restore"]["injected_disconnects"] for r in results):
        add("### 切断注入と差分復元（#35）")
        add("")
        add("| エンジン | 注入した切断 | 復帰 中央値 / 最大 | 配信seq | 復元されなかった | 履歴外（仕様どおり） | 想定外の切断 | 復元失敗 |")
        add("|---------|------------|------------------|--------|----------------|------------------|------------|--------|")
        for r in results:
            res = r.get("restore")
            if not res:
                continue
            mark = " ⚠️" if res["gaps"] else ""
            add(
                f"| {r['engine']} | {res['injected_disconnects']}回 | "
                f"{res['restore_median_s']}s / {res['restore_max_s']}s | "
                f"{res['published_seqs']} | {res['gaps']}件{mark} | "
                f"{res['beyond_history']}件 | {r['disconnects']} | {r['reconnect_failures']} |"
            )
        add("")
        add(f"- 注入間隔 {results[0].get('disconnect_every_s')}秒。ランダムに1名を選び close code 1001")
        add("  （going away＝端末スリープ相当）で切る。**生徒側の再接続実装は触っていない**")
        add("- 「復元されなかった」= 履歴上限（`history_resend`）の**内側**なのに最後まで")
        add("  届かなかった seq。**0 でなければ再接続の差分復元が効いていない**")
        add("- 「履歴外」= 履歴上限より古いぶんの欠落。`joined.history_from` が「これより前は")
        add("  恒久欠落」と伝える契約どおりで、**不合格の材料にしない**")
        add("- 「想定外の切断」は注入したぶんを**含まない**。含めると注入するほど不合格に近づく")
        add("- 音声送出が終わった時点で注入を止め、drain の間に最後の復元を完了させている")
        add("")
    add("### 推論回数（#32）")
    add("")
    add("| エンジン | ASR（確定） | ASR（partial） | Hy-MT2/NLLB | キャッシュヒット | 生存タスク |")
    add("|---------|-----------|--------------|------------|--------------|----------|")
    for r in results:
        c = r.get("calls") or {}
        cache = r.get("mt_cache") or {}
        add(
            f"| {r['engine']} | {c.get('asr', '-')} | {c.get('asr_partial', '-')} | "
            f"{c.get('mt', '-')} | {cache.get('hits', '-')} | {c.get('tasks', '-')} |"
        )
    add("")
    add("- 生徒人数に対する回数の不変性はスケーリングベンチ（`scripts/scale_bench.py`）で測る")
    add("")
    add("- 初回字幕 = 配信開始→最初のcaption到達。**最初の発話が終わるまでの待ちを含む**")
    add(f"- CPU はサーバープロセスツリー合計（1コア=100%、論理{results[0].get('cpu_percent', {}).get('cores', '?') if results[0].get('cpu_percent') else '?'}コア）")
    add("- ASR滞留 = `audio_queue_seconds`（ASR待ち＋処理中の音声の秒数）。2秒間隔のサンプリング")
    add("")
    for r in results:
        if not r["verdict"]["passed"]:
            add(f"### {r['engine']} の不合格理由")
            add("")
            for reason in r["verdict"]["reasons"]:
                add(f"- {reason}")
            add("")
    # エラー明細（N-08 の切断・拒否・先生切断の内訳。件数は上表、内容はここ）
    if any(r["errors"] for r in results):
        add("## エラー明細")
        add("")
        for r in results:
            if r["errors"]:
                add(f"- **{r['engine']}**（{len(r['errors'])}件）")
                for err in r["errors"][:20]:
                    add(f"  - {err}")
                if len(r["errors"]) > 20:
                    add(f"  - …ほか {len(r['errors']) - 20} 件（詳細は .json 参照）")
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
    import platform

    return {
        "cpu": cpu,
        "cores": psutil.cpu_count(logical=False),
        "threads": psutil.cpu_count(logical=True),
        "ram_gb": round(psutil.virtual_memory().total / 1e9, 1),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "date": datetime.date.today().isoformat(),
    }


def update_config_default(engine: str) -> None:
    """合格したエンジンを config.yaml の既定 mt.engine に反映する。

    値が hy-mt2/nllb の engine 行（＝mt.engine）だけを対象にする（asr=faster-whisper,
    vad=silero は値が違うため一致しない）。mt.engine が fake 等でこの正規表現に
    一致しない場合は書き換えられないので、その旨を警告する。
    """
    import re

    path = ROOT / "config.yaml"
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(
        r'(?m)^(\s*engine:\s*)("?)(hy-mt2|nllb)("?)(\s*(?:#.*)?)$',
        lambda m: f"{m.group(1)}{engine}{m.group(5)}",
        text,
        count=1,
    )
    if n == 0:
        print(
            f"警告: config.yaml の mt.engine を自動更新できませんでした"
            f"（現在の値が hy-mt2/nllb でない可能性）。手動で mt.engine: {engine} に設定してください。"
        )
        return
    if new != text:
        path.write_text(new, encoding="utf-8")
        print(f"config.yaml の mt.engine を {engine} に更新しました。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=45, help="試験長（分・既定45）")
    parser.add_argument("--students", type=int, default=10, help="擬似生徒数（既定10）")
    parser.add_argument("--audio", default=None, help="リプレイする録音WAV（16kHz mono）")
    parser.add_argument(
        "--corpus", default="ja", choices=["ja", "ja_ext"],
        help="--audio 未指定時にループする合成音源（既定 ja。ja_ext は #22 の拡張コーパス）",
    )
    parser.add_argument(
        "--langs", default=None,
        help="生徒の言語をカンマ区切りで指定（既定は config.yaml の languages）",
    )
    parser.add_argument(
        "--disconnect-every-s", type=float, default=None,
        help="N秒ごとにランダムな生徒1名の接続を切る（#35 の切断注入。既定は注入しない）",
    )
    parser.add_argument(
        "--mt-cache-size", type=int, default=None,
        help="翻訳キャッシュの上限件数を上書き（0 = 無効＝MT負荷の上界を測る・#32）",
    )
    parser.add_argument("--engine", default=None, choices=["hy-mt2", "nllb"], help="エンジン固定")
    parser.add_argument(
        "--asr", default=None, choices=["faster-whisper", "sherpa"],
        help="ASRエンジン固定（#28 の A/B 用。既定は config.yaml の値）",
    )
    parser.add_argument(
        "--partial", default=None, choices=["on", "off"],
        help="partial字幕の有効/無効を固定（#29 の A/B 用。既定は config.yaml の値）",
    )
    parser.add_argument("--port", type=int, default=ACCEPT_PORT)
    parser.add_argument("--drain-seconds", type=float, default=20)
    parser.add_argument("--out-dir", default=str(ROOT / "docs" / "accept"))
    parser.add_argument("--no-config-update", action="store_true", help="合格構成をconfigへ反映しない")
    args = parser.parse_args()
    # "on"/"off" → bool/None（None なら config.yaml の値のまま）
    args.partial = None if args.partial is None else args.partial == "on"
    args.langs = [c.strip() for c in args.langs.split(",")] if args.langs else None

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
