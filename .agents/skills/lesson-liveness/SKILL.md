---
name: lesson-liveness
description: 音声入力から翻訳字幕までの反復、MT満杯後の再開、推論失敗後の継続を実コードで検証する。Use when changing Pipeline feed_audio/drain/workers, CaptionDelivery live admission/wakeup/worker, or investigating captions stopping after repeated utterances. Includes real-model repetition and student DOM checks.
---

# 授業の字幕処理を継続する

受理した音声の認識・翻訳が成功すれば、各言語の生徒へ対応する字幕が一度ずつ届き、排出後も次の音声を処理できる。
違反すると、授業の途中から生徒の翻訳字幕が更新されなくなる。

## 有界探索を実行する

リポジトリ直下で実行する。Python 3.12.10 と既存の固定依存を使う。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/lesson_liveness.py --mutation-check --json .audit/lesson-liveness.json --screens .audit/lesson-liveness-screens.json
```

同じ依存がある環境では `.venv/Scripts/python.exe` も使える。
Windowsの制限環境でasyncioの内部socketpair生成が止まる場合は、実行権限を確認して同じコマンドを再実行する。
終了コード0、`status: PASS`、`attempted: 19531`、`legal: 413`、固定6ケース、6破壊すべての `detected: true` を確認する。
`--case feed,asr_ok,mt_fail` や `--witness capacity` は単独再現用とし、変更後の最終確認には既定の全探索を使う。
`FAIL` の `property`、`trace`、`expected`、`actual` を読み、[操作と保証範囲](../../../scripts/formal/lesson_liveness.md) と製品コードを照合する。
Traceback、プロセスの期限超過、破壊の置換箇所不一致は検証器か環境の失敗として調査する。

## 生徒画面を確認する

表示処理を変更した場合と、字幕が途中から表示されない症状を調べる場合は、保存したフレームを実ブラウザーへ渡す。
Windowsのインストール済みEdgeとPlaywright 1.64.1を使う。

```powershell
uv run --python 3.12.10 --no-project --with playwright==1.64.1 python -X utf8 -B scripts/formal/lesson_liveness_view.py .audit/lesson-liveness-screens.json --mutation-check --json .audit/lesson-liveness-view.json
```

`status: PASS`、全画面の `visible: true`、`display_stops_after_16` と `hidden_after_16` の `detected: true` を確認する。
実際の生徒ページを使い、WebSocketの受信フレームだけを再生する。マイクと実ネットワークは含まない。

## 実モデルの反復を確認する

モデルを変更した場合と、モデル内部の反復で停止する疑いがある場合は、モデルと通常のアプリ依存がある環境で実行する。
音源がなければ `scripts/make_fixture_audio.ps1` で生成する。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/lesson_liveness_real.py --json .audit/lesson-liveness-real.json --screens .audit/lesson-liveness-real-screens.json
.venv/Scripts/python.exe -X utf8 -B scripts/formal/lesson_liveness_view.py .audit/lesson-liveness-real-screens.json --json .audit/lesson-liveness-real-view.json
```

`create_app` の未注入経路でASR・MTを別プロセスに構築し、同じPipelineとモデルで翻訳キャッシュを無効にして32回処理する。
`status: PASS`、英中各32字幕、`pipeline_stop_returned: true`、`all_children_reaped: true` とコマンドの終了コード0を確認する。
有限回の観測であり、形式証明や授業時間中の性能保証として扱わない。

## 実装との同期を確認する

最終同期コミットは `5aa0ca374beb563376b247eee60fd811e954e977`。

| 対象 | 照合する処理 |
|---|---|
| `server/pipeline.py` | start、feed_audio、_asr_worker、_process_segment、_publish_turn、drain、stop |
| `server/caption_delivery.py` | publish、_enqueue_mt、_mt_worker、_translate、_process_mt_job、drain |
| `server/audio/ingest.py`、`server/audio/vad.py` | PCM変換とEnergyVADによるSegment確定 |
| `server/asr_backlog.py`、`server/inference.py`、`server/turn/assembler.py` | 単一消費者、Future完了・失敗、simpleのTurn確定 |
| `server/caption_history.py`、`server/mt/cache.py` | 採番、履歴失効、翻訳キャッシュ |
| `server/roster.py`、`server/delivery.py` | 言語別受信者とClientSenderの送信 |
| `server/config.py`、`config.yaml` | 容量、期限、既定モデル構成 |
| `server/main.py`、`server/inference_process.py`、`server/asr/haas_onnx_engine.py`、`server/mt/hymt_engine.py` | 別コマンドで32回観測するプロセス構築・認識・翻訳・停止 |
| `web/index.html`、`web/student.js`、`web/caption-recovery.js` | 別コマンドで実行する受信とDOM表示 |

```powershell
git diff 5aa0ca374beb563376b247eee60fd811e954e977 -- server/pipeline.py server/caption_delivery.py server/audio server/asr_backlog.py server/inference.py server/turn/assembler.py server/caption_history.py server/mt/cache.py server/roster.py server/delivery.py server/config.py server/main.py server/inference_process.py server/asr/haas_onnx_engine.py server/mt/hymt_engine.py config.yaml web/index.html web/student.js web/caption-recovery.js
```

実行順序、await境界、容量、失敗時の処理単位が変わったら、操作列と期待値を見直す。
破壊は一時コピーの製品コードへ適用し、元の製品ファイルを変更しない。
新実装との一致、通常版の成功、各性質の破壊検出を確認した後だけ、同期コミットと検証器の `SYNC_COMMIT`、基準結果を更新する。
未確認の変更から同期を戻す場合は `/maintain-formal-verification-skill` を使う。
実コードの反例は保持し、保証を弱めて消さない。製品コードの修正は依頼された場合に行う。

## 保証の限界を守る

Python実装に対する有限操作列の全列挙と長い固定ケースであり、SMT検証や無制限の形式証明ではない。
計算は決定的な代役、VADはenergy、Turnはsimpleとする。実モデルと画面は別コマンドで確認する。
保留した推論が最終的に返り、正常な接続へ送信できることを前提とする。
有界探索へ注入するスレッド版Runnerのタイムアウトは待機だけを終了する。
本番のプロセス版Runnerの永久ハング復旧は [lesson-endurance](../lesson-endurance/SKILL.md) で別に確認する。
マイク、実WebSocket、LAN、実時間での入力速度、精度、50分授業、メモリー上限、全asyncio順序の網羅は対象外。
先生交代、再接続復元、録音保存、ASR容量超過の規則は既存の各スキルを併用する。
