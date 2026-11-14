# 先生操作の追加検証

既存14本の対象外から、先生ページのマイク要求の所有権を次の対象に選んだ。古い取得要求の成功が新しい選択を上書きすると、先生が選んでいない機器から音声を送るか、画面が配信中でも字幕が止まる。既存 `teacher-audio` は復旧前後の音声到達を確認するが、取得Promiseを即時完了させるため競合は生成しない。

指定された終了ボタンからの保存再試行も別のスキルにした。既存 `lesson-recording` はサーバーの `Lesson.control` を直接呼ぶため、画面の終了ボタンが無効で再試行要求を送れない不具合を検出できない。

## 実コードを有限範囲で実行する

Node.js 24.22.1で実 `web/teacher.js` を動かし、外部APIの完了と画面操作を制御する。保存側はPython 3.12.10の実 `Connection`、`Lesson`、`SessionRecorder` まで通す。独立したTLA+モデルへ製品の分岐を写す方法より、画面のボタン活性とPromiseの所有権を直接検査できるこの方法を選んだ。SMTによる証明や無制限の形式証明ではない。

`Model the Domain` に従い、操作列と非同期完了列、外部から観測した結果、性質違反を検証のデータにした。`Prove It Works` に従い、実製品ファイルの一時コピーに破壊を加え、想定した性質の違反を検出し、元の結果へ戻ることを要求する。

同期コミットは `df2dd6e93f83fd85a5186898071392599b7e2875`。製品コードは変更していない。再実行のコマンド、対象関数、操作集合、制限はそれぞれのスキルを参照する。

- [teacher-microphone](../../.agents/skills/teacher-microphone/SKILL.md)
- [teacher-recording-retry](../../.agents/skills/teacher-recording-retry/SKILL.md)

## 基準結果

| 検証 | 既定範囲の結果 | 実コードの破壊と復元 |
|---|---|---|
| マイク要求 | 4初期状態、選択最大2回、開始最大1回。合法9,428prefixがすべて成功 | 8破壊を指定性質・終了コード1で検出。復元後PASS・結果一致 |
| 保存再試行 | 2障害位置、通知受信と喪失、3操作の長さ0〜3。160候補中80完走、80候補は同じ短い反例で停止 | 通知受信80候補の正常対照がPASS。13破壊を検出。復元後も既知反例を含むFAIL・結果一致 |

実行結果は [マイク基準JSON](teacher_microphone_baseline.json) と [保存基準JSON](teacher_recording_retry_baseline.json) に保存する。マイクのprefix数は異なる状態数ではない。保存の失敗80候補は、障害位置ごとの2つの短い反例にまとめている。失敗位置より後の操作は実行していない。

```powershell
node scripts/formal/teacher_microphone.cjs --mutation-check --json .audit/teacher-microphone.json
.venv/Scripts/python.exe -X utf8 -B scripts/formal/teacher_recording_retry.py --mutation-check --json .audit/teacher-recording-retry.json
```

2つ目は現在、以下の製品反例により終了コード1を返す。固定依存環境を作るコマンドは保存側スキルに記載する。

## 保存失敗通知を失うと終了ボタンから再試行できない

保存側で [Issue #194](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/194) の反例を確認した。

1. 記録ONの授業で発話を確定し、先生が終了ボタンを押す。
2. 保存が失敗する。旧接続の `session: ended` までは届くが、その後の `recording_save_failed` が切断で失われる。
3. 同じ先生ページが再接続する。サーバーの `joined` は `ended` を返すが、保存失敗を再通知しない。
4. 先生ページの `recordingSaveError` は空のままで、終了ボタンが無効になる。終了ボタンから保存を再試行できない。

保存前の失敗と、JSONL保存後・Markdown保存前の失敗で再現する。通知を受信してから切断する対照では再試行できる。開始ボタンにもサーバー側の保存再試行経路があり、今回データ消失を観測したわけではない。終了ボタンと保存失敗通知の復元が不足している。

検証結果の `FAIL` と終了コード1を保持する。既知の反例であることを理由に成功扱いにしない。製品修正後は同じ操作列が成功することを確認し、基準結果と同期コミットを更新する。

## 既存検証との境界

マイクの物理機器、OS固有の中断条件、デバイス一覧の遅い応答、全ブラウザーの振る舞いは保証しない。初回の音声グラフ作成中の多重開始も別の範囲である。PCM変換と音声から字幕までの継続は既存 `teacher-audio` に委ねる。

保存側は保存失敗と再試行の経路を対象にする。実ディスクの破損、プロセスクラッシュ、全WebSocketスケジュール、未完了ASRの全順序は保証しない。排出待ちと授業間の記録の所属は既存 `lesson-recording` に委ねる。

既存の先生音声・記録画面テスト15件と、授業・記録器テスト40件も成功した。これらの通常テストの成功は、追加した探索範囲の成功を意味しない。
