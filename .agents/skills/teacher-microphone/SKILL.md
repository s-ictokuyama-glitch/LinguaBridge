---
name: teacher-microphone
description: 先生ページのマイク再取得・選択競合・既定への切替・取得拒否を実JavaScriptの有界スケジュール探索で検査する。Use when changing teacher.js requestMic, ensureMic recovery, attachStream, microphone selection, or AudioContext resume coordination.
---

# マイク再取得と選択の競合を確認する

先生の最新のマイク選択だけを入力へ反映し、再開は利用可能な入力を確保してから送信する。違反すると、選んだ機器と異なる音声を送る、無音のまま配信を開始する、使わないマイクを取得したまま残す。

[teacher-audio](../teacher-audio/SKILL.md) が対象外としていた、取得・Context再開の未完了Promiseと選択操作の交錯を担当する。初回取得済みの状態から検査する。

## 一度のコマンドで実行する

リポジトリ直下で Node.js 24.22.1 を使う。外部パッケージは不要。

```powershell
node scripts/formal/teacher_microphone.cjs --mutation-check --json .audit/teacher-microphone.json
```

終了コードとJSONの `status` を読む。0は `PASS`、1は性質違反の `FAIL`、2は環境・検証器の `ERROR`。`FAIL` を期待値として成功扱いにしない。検証器の例外も指定先JSONへ保存するが、保存先自体へ書き込めない場合は標準出力の `output_error` を読む。

[基準結果](../../../scripts/formal/teacher_microphone_baseline.json) は `PASS`。候補9,800件、違法372件、合法9,428件、成功9,428件、違反0件。これは操作列の各prefixの件数であり、異なる状態数ではない。未完了APIがないprefixは5,886件。正常探索・8破壊・復元を合わせて、確認したWindows環境では約12秒だった。実行時間は性能保証ではない。

`mutations.results` は全8件の `detected: true` と `exit_code: 1` を要求する。`restored: PASS` と `restored_matches: true` は、一時コピーを復元した探索が正常対照と件数・結果まで一致することを示す。元の製品ファイルは変更しない。

## 何を全探索するか

Node VMで実 `web/teacher.js` を読み、実 `init` が登録した開始・マイク選択callbackを発火する。別言語に実装を写すモデルより、実コードの所有権確認・待機・停止処理を直接検査する方法を選んだ。SMTや無制限の形式証明ではない。

- 初期機器は `mic-a`。trackのlive/endedとContextのrunning/suspendedの4組を使う。
- 授業は参加済み・一時停止、初回取得済み。開始は最大1回、選択は最大2回。候補機器は `mic-a`、`mic-b`、既定の3値。
- 現在表示されている値と同じ選択にはchangeが起きないため、その候補を違法として除外する。未完了選択を前の機器へ戻す操作は含む。disabledのcontrolは発火しない。
- 保留中の `getUserMedia` と `AudioContext.resume` の任意の1件を成功・失敗させ、全合法順序を生成する。再取得の指定機器失敗後に生まれる既定取得も含む。要求の未完了prefixも検査し、古い要求が未完了のまま最新要求だけ成功した場合の進行を要求する。
- 外部イベントを1つ実行した後は `setImmediate` まで待ち、microtaskを排出する。同期処理の `attachStream` 途中には割り込まない。Promiseを同じタスク内で複数決着させる順序は対象外。
- 期待値は利用者操作と外部API要求番号から独立した `Contract` で構築する。製品の `state.micRequest` を期待値には使わない。開始送信時の入力を保存し、後の選択を過去の開始の期待値へ適用しない。

## 違反と破壊検出を読む

`bounded.violations` は性質ごとの最短反例を、初期状態・操作順・期待値・実際値で示す。幅優先探索で最初に見つけた列を残す。失敗したprefixの先は探索しないので、失敗時の件数を正常時の網羅件数と比較しない。

| 性質 | 要求する結果 | 感度を確認する実コードの破壊 |
|---|---|---|
| `request_contract` | 選択は指定機器のみを要求し、再取得だけが失敗後に既定へ進む | 再取得の既定フォールバックを削除 |
| `input_ownership` | 最新成功streamだけが接続され、機器IDと保存値が一致する。失敗では旧入力を維持する | 古い成功結果の所有権確認を削除 |
| `superseded_cleanup` | 使わない取得済みstreamのtrackが停止する | 古い成功結果のtrack停止を削除 |
| `start_contract` | 最新要求待機中は開始せず、決着後はrunning Contextとlive入力で開始する | `ensureMic` の最新要求への追従を削除 |
| `selection_display` | 未完了時は選択した値、決着後は実入力を選択欄に表示する | 一覧更新時に別機器の値を表示 |
| `fallback_notice` | 既定機器への再取得を明示する | フォールバック通知を削除 |
| `selection_failure_notice` | 選択失敗と旧入力維持を明示する | 選択失敗の通知を削除 |
| `controls_available` | 参加済み・一時停止で再開操作ができる | 開始ボタンを常時disabledにする |

例として `select:mic-b → select:mic-a → complete:0:resolve` は、Bの取得開始後にAへ戻し、Bの古い取得だけが成功する順序。所有権確認を削除するとBが入力へつながる。正常実装はBのtrackを停止し、Aの取得を待つ。

取得拒否の反例では、環境が意図的に拒否したこと自体を不具合にしない。拒否後に開始した、旧入力を失った、選択失敗を隠した、といった契約違反を読む。未知の例外、変更により一致しなくなった破壊用文字列、ブラウザー代役の不足は `ERROR` として調べる。新しい製品反例は保持し、性質を弱めない。製品修正は依頼されたときに行う。

## 実装との同期を保つ

同期コミットは `df2dd6e93f83fd85a5186898071392599b7e2875`。

| ファイル・関数 | 確認する対応 |
|---|---|
| `web/teacher.js` の `init`、`setButtons`、`sendControl` | クリック・changeの登録、参加済み操作、開始送信 |
| 同 `requestMic`、`getMicStream` | 要求の所有権、古い結果の破棄、成功・失敗、既定フォールバック |
| 同 `ensureMic`、`hasLiveMic` | Context再開待ち、最新選択への追従、利用可能な入力の確認 |
| 同 `attachStream`、`storageGet`、`storageSet` | 同期的な接続交換、旧track停止、保存する機器 |
| 同 `onMicSelected`、`refreshMicList`、`showMicNotice` | 選択欄・保存値・通知。列挙は固定一覧を即時返す前提 |
| `scripts/formal/teacher_microphone.cjs` | イベント生成、独立契約、Media API・DOM代役、破壊検出 |

```powershell
git diff df2dd6e93f83fd85a5186898071392599b7e2875 -- web/teacher.js
```

対象コードを変更したらawait境界・同期単位・ボタン活性・API要求の意味を照合し、必要なら生成器と契約を更新する。正常対照、全破壊の指定違反と終了コード、復元を確認した後だけ、この同期コミットと検証器の `SYNC_COMMIT`、基準結果を進める。基のブランチから到達可能で、確認した製品内容を持つコミットを選ぶ。未確認の変更が溜まった場合は `/maintain-formal-verification-skill` を使う。

## 保証しない範囲

初回取得・初回並行開始、複数の開始クリック、拒否後の再クリック、取得後にtrackが終了する追加イベント、機器一覧更新の競合、実機器の抜去、実ブラウザー・OSの全動作、ネットワーク・字幕・AudioWorkletは対象外。ストレージ、sourceのconnect/disconnect、track.stopは正常に完了するものとする。初回取得から字幕到達と実ブラウザーの音声経路は既存 `teacher-audio` で確認する。終了ボタンからの授業記録保存再試行は、このマイク検証の保証には含めない。
