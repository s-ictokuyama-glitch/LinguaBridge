---
name: morph-lesson
description: 既定morphの音声入力から英中字幕まで、長無音・文末・一時停止による確定とMT満杯後の継続を実コードの有界探索と50分仮想入力で検査する。Use when changing Pipeline boundary/flush dispatch, VoiceSegmenter morph events, AsrBacklog ordering, TurnAssembler integration, or sustained morph caption delivery.
---

# 文中の区切りを含む授業の字幕継続を確認する

「温度が高いので」のような認識断片を保留しても、長い無音・後続の文末・一時停止で確定し、英中字幕へ順序どおり一度ずつ届ける。
違反すると、音声を受信していても生徒の字幕が止まるか、別の発話と結合する。

既存の [lesson-endurance](../lesson-endurance/SKILL.md) はsimple戦略、[turn-assembly](../turn-assembly/SKILL.md) は組立器への直接入力を使う。この検証は既定morphのPCM、VAD、ASR待ち、Turn確定、翻訳の接続部分を扱う。

## 全検証を実行する

リポジトリ直下でPython 3.12.10と固定依存を使う。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/morph_lesson.py --mutation-check --json .audit/morph-lesson.json
```

同じ依存がある場合は `.venv/Scripts/python.exe -X utf8 -B scripts/formal/morph_lesson.py --mutation-check --json .audit/morph-lesson.json` でも実行できる。
Windowsの制限環境でasyncio初期化が停止する場合は、ローカル通信の実行権限を確認する。環境の停止を製品の反例と数えない。

終了コード0と `status: PASS`、4,681候補の `legal_passed: 1548`、`failed: 0`、固定5ケースの成功を確認する。
`sustained` は3000秒の2構成で、それぞれ30,000入力フレーム、1,500音声断片、1,200確定発話、英中各1,200字幕を比較する。発話ごとの排出待ちは行わない。
`mutations` の9件すべてに `healthy: PASS` と `detected: true` があり、`restored: PASS`、`restored_matches: true` であることを確認する。
[基準結果](../../../scripts/formal/morph_lesson_baseline.json) と [範囲・選定理由](../../../scripts/formal/morph_lesson.md) を参照する。

`--bounded-only` は長時間入力を省く。`--sustained-only --seconds 150` は短い連続入力、`--witness boundary|flush|backpressure|count|span` は指定した固定ケースだけを実行する。`--witness` には列挙した名前を1つ渡す。短縮実行を最終確認の代用にしない。

## 反例を判断する

終了コード1と `status: FAIL` は性質の違反、2と `ERROR` は検証器または環境の失敗を示す。終了コード1を期待するテストとして包み、現行製品の失敗を成功扱いにしない。元コードがFAILなら反例を保持し、破壊検出は実行しない。
`violations` の `property`、`trace`、`expected`、`actual` を読む。

- `morph_turns` は先生の確定発話の内容・結合・順序・重複を比較する。末尾flush前にも判定する。
- `bilingual_delivery` は英中の字幕内容・順序・重複と接続継続を比較する。
- `drain_completion` は受理済み推論の完了後、ASRと翻訳の排出が終わることを要求する。
- `sustained_deadline` は入力計画で指定した確定時点から8仮想秒以内の字幕数を要求する。
- `retained_state_bound` は指定した待ち行列、保留断片、履歴、キャッシュ、受信記録、タスク数の上限を確認する。
- `audio_admission` はこの処理速度と音声計画でASR容量による破棄がないことを要求する。
- `worker_liveness` はASR、MT、統計ワーカーの途中終了を検出する。

`backpressure_reached` は固定ケースでMT満杯と先生への通知を作れたことの検査である。失敗時は設定と入力計画の対応も調べる。
破壊の置換不一致、子プロセスの例外や時間切れ、JSONなしは検出成功に数えない。
ASR完了と境界の実際の順序、100ms単位の音声計画、入力台帳の期待値を実コードと照合する。前提が一致する反例は保持し、製品修正は依頼された場合に行う。

## 実装との同期を確認する

同期コミットは `1840857a512024404c3504cbe4465afb5d952c45`。

| 対象 | 照合する契約 |
|---|---|
| `server/pipeline.py` | `feed_audio`、`flush_audio`、`_asr_worker`、`_close_pending_turn`、`_process_segment`、`_publish_turn`、`drain`、開始と停止 |
| `server/audio/vad.py`、`server/audio/ingest.py` | energy判定、PCM変換、500msのSegment、1000msのTurnBoundary、flush |
| `server/asr_backlog.py` | 確定音声と境界のFIFO、容量、完了会計 |
| `server/turn/assembler.py`、`server/turn/boundary.py` | 継続語尾と終止語尾、12断片・30秒上限、境界とflush |
| `server/caption_delivery.py`、`server/caption_history.py`、`server/mt/cache.py` | 翻訳の背圧、起床、排出、確定字幕、保持量 |
| `server/inference.py`、`server/asr/base.py`、`server/asr/hallucination.py`、`server/mt/base.py` | Futureの計算完了、空結果・失敗の扱い |
| `server/roster.py`、`server/delivery.py`、`server/ws_protocol.py` | 先生と英中生徒への送信と字幕形式 |
| `server/config.py`、`config.yaml` | 出荷morph設定、音声・翻訳の容量と期限 |
| `scripts/formal/lesson_liveness.py`、`scripts/formal/lesson_endurance.py` | 再利用するExecutor、Socket、Violation、callback処理、仮想時計 |

```powershell
git diff 1840857a512024404c3504cbe4465afb5d952c45 -- server config.yaml scripts/formal/lesson_liveness.py scripts/formal/lesson_endurance.py
```

待機境界、フレーム長、確定条件、配信形式が変わったら、入力台帳と故障注入を照合する。実コードとの一致、全探索、50分2構成、破壊検出と復元を確認した後だけ、この同期コミット、検証器の `SYNC_COMMIT`、基準結果を更新する。
同期先には対象ファイルと同じ内容を持つ基のブランチのコミットを使う。新規cloneでSHAが解決し、差分照合できることを確認する。未確認の変更が溜まった場合は `/maintain-formal-verification-skill` を使う。

## 保証の限界を守る

Python実コードへの有限入力の全列挙であり、SMTやTLA+による証明、無制限の保証ではない。8操作の長さ0〜4の全列から、完了対象のFutureがある列を検査する。各操作後は16回イベントループを進める。任意のasyncio順序の網羅ではない。
無音と音声の判定はenergy、ASRとMTの計算は入力IDを返す代役。出荷設定のmorph、surface、500ms分割、1000ms確定、240msプリロール、12断片・30秒上限を使う。MT容量と再送抑制閾値は1へ縮めて背圧を到達可能にする。統計配信間隔は3600秒とし、統計の定期配信は対象外とする。
50分は仮想時間であり実時間の性能を測らない。ASR/言語ごとのMTは0.1/0.2秒と0.8/0.8秒の2構成、末尾は10仮想秒を排出に使う。上限は観測したコンテナーだけで、プロセス全体のメモリー量ではない。
Silero判定、実モデルの精度・速度、物理マイク、AudioWorklet、WebSocket受信、LAN、生徒DOM、先生交代、授業記録、partial、推論ハング復旧は対象外。該当する既存スキルを併用する。実時間50分性能は [lesson-endurance](../lesson-endurance/SKILL.md) の実モデルコマンドで別に確認する。
