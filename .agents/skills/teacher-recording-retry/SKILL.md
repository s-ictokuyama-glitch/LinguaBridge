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

基準結果は [teacher_recording_retry_baseline.json](../../../scripts/formal/teacher_recording_retry_baseline.json)。現在は `FAIL` で、[Issue #194](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/194) の反例を保持する。期待する成功条件を弱めたり、終了コード1を成功扱いにしたりしない。
`--suite delivered` は初回失敗通知を受信できた正常対照だけを調べる。これが成功しても全体の保証にはならない。
`--depth 0` は最短反例の切分け用。全体確認と破壊検出には既定の深さ3を使用する。

## 範囲と実コードの対応

`teacher_recording_retry.cjs` は実 `web/teacher.js` を Node の vm で読み、`init` が登録したクリックハンドラーを実行する。無効なボタンはクリックを配送しない。
クリックから出た JSON を Python の実 `Connection.receive` へ渡し、実 `Lesson.control`、`SessionRecorder.write` を通す。実 Roster の送信した payload を同じ画面の message ハンドラーへ戻す。サーバーの状態遷移を JavaScript 側で再実装しない。

初期条件は先生1人、記録ON、確定発話1件。保存前または最初の Markdown 書込み前に `OSError` を注入する。
初回通知は受信または切断で損失の2条件。損失時は同じ接続の最初の `recording_save_failed` より前だけをFIFO順に配信し、残りをすべて破棄する。旧接続を `Lesson.leave` で終了して新しい `Connection` から参加し直す。
その後、`retry`、`disconnect`、`warning` の長さ0〜3の全40列を各条件で実行する。計160候補を調べ、反例が出た候補はその位置で止める。
受信条件の80候補は完走する。損失条件の80候補は共通の短い接頭列で停止するため、80種類の異なる反例ではない。JSON の `candidate_count` が同じ反例に到達した候補数を示す。
障害中の開始拒否、障害解消後の終了クリック、成功後の終了再クリック、次授業の確定発話1件と終了も各完走候補の末尾で確認する。
発話時刻は固定し、同じ分に始まる2授業のフォルダ衝突も決定的に検査する。

## 反例を判断する

`violations` の `property`、`trace`、`expected`、`actual` を読む。最短の現行反例は次の順序になる。

1. 先生が開始し、授業の発話1件が確定する。
2. 終了ボタンからサーバーへ `control/end` を送る。サーバーは ended になり、保存は失敗する。
3. ended の通知を受け取り、保存失敗通知を受け取る前に切断する。
4. 同じ先生ページが再接続して joined/ended を受け取る。
5. 終了ボタンが無効で、保存再試行の `control/end` を送れない。

入力が製品で成立すること、切断で失われるメッセージのFIFO境界、awaitの境界を確認する。違反が製品に由来するなら反例を残す。製品の修正を依頼されていない場合は検証追加だけを行う。

## 検出器を確認する

`--mutation-check` は通知受信条件の全80候補を正常対照にし、`server/` と `web/` の一時コピーを1箇所ずつ壊す。元の製品コードは変更しない。
各 `mutations.results` は `detected: true`、終了コード1、対応する性質の違反をすべて要求する。置換箇所が一意でない、構文が壊れる、性質以外の例外になる場合は検証器エラーとする。

| 性質 | 実コードへの破壊 |
|---|---|
| `retry_available` | 保存失敗時のボタン有効化を削除、または切断時に再試行状態を忘れる |
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

13破壊の検出後に元ソースの全160候補を再実行する。`restored: FAIL` と `restored_matches: true` は既知反例を含む同じ基準に戻ったことを表す。全体PASSへの復旧を意味しない。
`start_command` は fixture と次授業操作の到達を確かめる補助assertであり、マイクの開始自体は本スキルの保証に含めない。

## 変更との同期を確認する

最終同期コミットは `df2dd6e93f83fd85a5186898071392599b7e2875`。検証器の `SYNC_COMMIT` と一致させる。

| ファイル | 照合する処理 |
|---|---|
| `web/teacher.js` | `init` の終了・開始クリック、`connect`、`handleMessage`、`applySessionState`、`setButtons`、`clearRecordingSaveWarning`、`sendControl` |
| `server/connection.py` | `receive` の参加・control配送、現在の先生の認可 |
| `server/lesson.py` | `join`、`leave`、`control`、`_transition`、`_write_recording`、`_finalize_lesson`、`_finish_recording` |
| `server/recorder.py` | `add`、`write`、`_write_markdown`、`_written` と `_session_dir` の保持 |
| `server/roster.py`、`server/delivery.py` | 先生への通知のキューと配送、接続の回収 |
| `server/pipeline.py`、`server/caption_history.py` | 確定Turnの記録器への配送、flushとdrain、発話時刻 |

`server/config.py`、`server/ws_protocol.py`、`server/asr/fake_engine.py`、`server/mt/fake_engine.py`、`server/turn/assembler.py` はfixtureの補助依存。設定構築、通信の型、確定Turnの生成方法が変わった場合は検証器の入力が引き続き実装で成立するか確認する。ASR・翻訳エンジンの品質やTurnの認識は保証しない。

```powershell
git diff df2dd6e93f83fd85a5186898071392599b7e2875 -- web/teacher.js server/connection.py server/lesson.py server/recorder.py server/roster.py server/delivery.py server/pipeline.py server/caption_history.py
```

変更時は操作集合、通知の構造、障害注入点、期待する所属を確認する。実コードとの一致と全探索・破壊検出を確認した後だけ同期コミットを進める。未確認の変更から追いつく場合は `/maintain-formal-verification-skill` を使う。

## 保証の限界

実装に有限操作列を入力する有界検証であり、無制限の形式証明ではない。独立したTLA+モデルを作らず、JavaScriptとPythonの実コードを動かすことで画面とサーバーの接続漏れも検出する。
各controlは完了まで待つ。任意の同時control、全Promise順序、3件以上の発話、記録ON/OFF、未完了ASR・翻訳、実ディスク破損、OSクラッシュ、実WebSocket/LAN、ページ再読込み、実DOM描画、物理マイクは対象外。
Media API、DOM、通信は代役を使い、接続の切断を指定位置で起こす。遅着ASRの保存は [lesson-recording](../lesson-recording/SKILL.md)、マイク競合は [teacher-microphone](../teacher-microphone/SKILL.md) を併用する。
