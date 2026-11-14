---
name: teacher-recording-retry
description: 終了ボタンからの保存再試行、保存障害と再接続、次授業への記録分離を実JSとPythonの有界探索で確認する。Use when changing teacher.js recording errors or end-button state, Connection control dispatch, Lesson recording finalization or join snapshots, or SessionRecorder retry behavior.
---

# 終了ボタンから授業記録を保存し直せることを確認する

保存に失敗した授業は終了状態を保ち、先生が再接続して終了ボタンを押すと同じ授業の記録を保存し直せる。
違反すると、先生が案内された再試行操作を実行できず、未保存の授業記録が残る。
保存成功後も前授業のファイルを保ち、次授業の発話は別の記録に保存する。

## 実行する

リポジトリ直下で Node.js 24.22.1 と Python 3.12.10 を使用する。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/teacher_recording_retry.py --mutation-check --json .audit/teacher-recording-retry.json
```

固定依存のある `.venv/Scripts/python.exe` も使用できる。
Windows の制限環境で asyncio の内部 socketpair が停止したら、通常のループバック通信を許可した環境で実行する。環境の停止を製品の反例に数えない。
0 と `PASS` は範囲内の反例なし、1 と `FAIL` は性質違反、2 と `ERROR` は環境または検証器の失敗を表す。終了コードと JSON の両方を確認する。 JSON保存先へ書き込めない場合は、標準出力の `output_error` を読む。

現在の製品コードの期待結果は `PASS` で、全160候補が完走する。
[teacher_recording_retry_baseline.json](../../../scripts/formal/teacher_recording_retry_baseline.json) は [Issue #194](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/194) の修正前の `FAIL` を比較用に保持する。現在の失敗と混同せず、期待する成功条件を弱めたり、終了コード1を成功扱いにしたりしない。
`--suite delivered` は初回失敗通知を受信できた正常対照だけを調べる。これが成功しても全体の保証にはならない。
`--depth 0` は最短反例の切分け用。全体確認と破壊検出には既定の深さ3を使用する。

## 範囲と実コードの対応

`teacher_recording_retry.cjs` は実 `web/teacher.js` を Node の vm で読み、`init` が登録したクリックハンドラーを実行する。無効なボタンはクリックを配送しない。
クリックから出た JSON を Python の実 `Connection.receive` へ渡し、実 `Lesson.control`、`SessionRecorder.write` を通す。実 Roster の送信した payload を同じ画面の message ハンドラーへ戻す。サーバーの状態遷移を JavaScript 側で再実装しない。

初期条件は先生1人、記録ON、確定発話1件。保存前または最初の Markdown 書込み前に `OSError` を注入する。
初回通知は受信または切断で損失の2条件。損失時は同じ接続の最初の `recording_save_failed` より前だけをFIFO順に配信し、残りをすべて破棄する。旧接続を `Lesson.leave` で終了して新しい `Connection` から参加し直す。
その後、`retry`、`disconnect`、`warning` の長さ0〜3の全40列を各条件で実行する。計160候補を調べ、反例が出た候補はその位置で止める。
修正前は受信条件の80候補が完走し、損失条件の80候補が共通の短い接頭列で停止した。80種類の異なる反例ではない。JSON の `candidate_count` が同じ反例に到達した候補数を示す。
現在は再接続の `joined.recording_save_error` から保存失敗の警告と終了ボタンを復元する。生徒の参加応答にはこの項目を含めない。成功後の先生の参加応答は `null` を返し、保存失敗の警告だけを解除する。旧サーバーの項目欠落では従来の再試行状態を保持する。
障害中の開始拒否、障害解消後の終了クリック、成功後の終了再クリック、次授業の確定発話1件と終了も各完走候補の末尾で確認する。
発話時刻は固定し、同じ分に始まる2授業のフォルダ衝突も決定的に検査する。

## 反例を判断する

`violations` の `property`、`trace`、`expected`、`actual` を読む。修正前の最短反例は次の順序になる。

1. 先生が開始し、授業の発話1件が確定する。
2. 終了ボタンからサーバーへ `control/end` を送る。サーバーは ended になり、保存は失敗する。
3. ended の通知を受け取り、保存失敗通知を受け取る前に切断する。
4. 同じ先生ページが再接続して joined/ended を受け取る。
5. 終了ボタンが無効で、保存再試行の `control/end` を送れない。

入力が製品で成立すること、切断で失われるメッセージのFIFO境界、awaitの境界を確認する。違反が製品に由来するなら反例を残す。製品の修正を依頼されていない場合は検証追加だけを行う。

## 検出器を確認する

`--mutation-check` は通知受信と通知喪失の全160候補を正常対照にし、`server/` と `web/` の一時コピーを1箇所ずつ壊す。元の製品コードは変更しない。
各 `mutations.results` は `detected: true`、終了コード1、対応する性質の違反をすべて要求する。置換箇所が一意でない、構文が壊れる、性質以外の例外になる場合は検証器エラーとする。

| 性質 | 実コードへの破壊 |
|---|---|
| `retry_available` | 保存失敗時のボタン有効化を削除、参加応答の保存失敗を無視、またはサーバーの保存失敗スナップショットを省略 |
| `warning_ownership` | 保存以外の警告まで消す |
| `disconnected_controls` | 切断中の終了ボタンを有効にする |
| `retry_command` | 終了クリックからの送信を削除 |
| `unsaved_retained` | 次授業へ進む前の保存を省略、またはMarkdown書込み前に保存済みとして封印 |
| `saved_complete` | Markdownの原文を空にする |
| `saved_destination` | 再試行のたびに保存先を割り当て直す |
| `saved_immutable` | 同じ分の既存授業フォルダを上書き |
| `recording_ownership` | 次授業で記録器を交換しない |
| `retry_reset` | 次授業でも前授業の再試行状態を保持 |
| `save_error_notified` | 保存失敗を別種のエラーとして送る |

14破壊の検出後に元ソースの全160候補を再実行する。`control: PASS`、全14件の `detected: true`、`restored: PASS`、`restored_matches: true` を要求する。
修正前の `forget_retry_on_disconnect` は参加応答から回復できるため、現在は画面のスナップショット適用とサーバーのスナップショット供給をそれぞれ壊す。元の終了再試行・記録保護の成功条件は維持する。
`start_command` は fixture と次授業操作の到達を確かめる補助assertであり、マイクの開始自体は本スキルの保証に含めない。

## 変更との同期を確認する

最終同期コミットは `69d06dfddfd1d36afd6f09e0cf45b4b95de08956`。検証器の `SYNC_COMMIT` と一致させる。

| ファイル | 照合する処理 |
|---|---|
| `web/teacher.js` | `init` の終了・開始クリック、`connect`、`handleMessage`、`applySessionState`、`setButtons`、`applyRecordingSaveError`、`clearRecordingSaveWarning`、`sendControl` |
| `server/ws_protocol.py` | `Joined.recording_save_error` の省略可能な文字列と `null` |
| `server/connection.py` | `receive` の参加・control配送、現在の先生の認可 |
| `server/lesson.py` | `join`、`leave`、`control`、`_transition`、`_write_recording`、`_failed_recording_message`、`_finalize_lesson`、`_finish_recording` |
| `server/recorder.py` | `add`、`write`、`_write_markdown`、`_written` と `_session_dir` の保持 |
| `server/roster.py`、`server/delivery.py` | 先生への通知のキューと配送、接続の回収 |
| `server/pipeline.py`、`server/caption_history.py` | 確定Turnの記録器への配送、flushとdrain、発話時刻 |

`server/config.py`、`server/asr/fake_engine.py`、`server/mt/fake_engine.py`、`server/turn/assembler.py` はfixtureの補助依存。設定構築、通信の型、確定Turnの生成方法が変わった場合は検証器の入力が引き続き実装で成立するか確認する。ASR・翻訳エンジンの品質やTurnの認識は保証しない。

```powershell
git diff 69d06dfddfd1d36afd6f09e0cf45b4b95de08956 -- web/teacher.js server/connection.py server/lesson.py server/recorder.py server/roster.py server/delivery.py server/pipeline.py server/caption_history.py server/ws_protocol.py
```

変更時は操作集合、通知の構造、障害注入点、期待する所属を確認する。実コードとの一致と全探索・破壊検出を確認した後だけ同期コミットを進める。未確認の変更から追いつく場合は `/maintain-formal-verification-skill` を使う。

容量1件の送信キューでの再参加、先生交代の待機中に起きる保存失敗、保存成功後の警告解除は `tests/unit/test_lesson.py` と `tests/unit/test_teacher_recording.py` でも検査する。参加応答に保存失敗を同梱し、再参加のたびに同期的な2件送信で容量超過しないことを確認する。任意のライブ通知が重なる場合の無切断を保証するものではない。

## 保証の限界

実装に有限操作列を入力する有界検証であり、無制限の形式証明ではない。独立したTLA+モデルを作らず、JavaScriptとPythonの実コードを動かすことで画面とサーバーの接続漏れも検出する。
各controlは完了まで待つ。任意の同時control、全Promise順序、3件以上の発話、記録ON/OFF、未完了ASR・翻訳、実ディスク破損、OSクラッシュ、実WebSocket/LAN、ページ再読込み、実DOM描画、物理マイクは対象外。
Media API、DOM、通信は代役を使い、接続の切断を指定位置で起こす。遅着ASRの保存は [lesson-recording](../lesson-recording/SKILL.md)、マイク競合は [teacher-microphone](../teacher-microphone/SKILL.md) を併用する。
