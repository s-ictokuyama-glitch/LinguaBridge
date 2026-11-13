# 形式検証と現行実装の同期

14本の形式検証を、`localServer2` の `4efcb9f6edd2b1e39aea9c173d9b8b29870861f1` と照合した。製品コードは変更していない。性質・探索上限・正常対照は維持した。

11本は `in sync`。`teacher-audio`、`lesson-endurance`、`inference-startup` は `updated`。製品の新しい反例は見つからなかった。耐久検証で発生した例外は検証器のPID通知の競合であり、製品の不具合として数えていない。先生音声の旧FAIL記述は、修正済みコードに対する文書・基準結果のずれだった。

終了コード、更新前後の状態、故障検出の一覧は [実行結果JSON](maintenance_20261114.json) に保存する。

## 実行結果と履歴

各スキルの対象ファイルを旧同期コミットから確認対象までdiffし、該当するコミットのメッセージを読んだ。関数移動と、同じ状態への新しい書込みも確認した。同期コミット未記録の検証はなかった。

以下の短縮SHAは、このブランチの履歴で解決できる。

- `1f5bc91` は推論のプロセス分離と停止時の回収を実装した #181。
- `4bdd9b2` はモデル準備期限と自動復旧を実装した #185。
- `e97e068` は同期SHAをマージ先から到達可能にした #187。
- `421edec` は先生音声検証を追加した #188。
- `0710fa3` はContext休止・track終了からの復旧を実装した #190。
- `4efcb9f` は先生画面の終了ボタンから保存を再試行可能にした #192。

`a15aea2` の前回の同期更新、`dcb0354` の字幕継続検証、`c130e62` の耐久検証、`e6cf7f9` の準備検証の追加も、関連する検証器の履歴として読んだ。

| 検証 | 判定 | 旧同期 | 確認した対象の変更 | 再実行と故障検出 |
|---|---|---|---|---|
| asr-backlog | in sync | `5381edb` | `1f5bc91`。単一ワーカー・容量・排出契約は不変 | 合法4,981列PASS、5破壊検出 |
| caption-recovery | in sync | `5381edb` | `1f5bc91`、`4bdd9b2`。停止処理と未使用の設定追加のみ | 1,568ケースPASS、7破壊検出 |
| client-delivery | in sync | `5381edb` | `1f5bc91`、`4bdd9b2`。送信・予約枠・容量は不変 | 合法1,211列と固定ケースPASS、10破壊検出 |
| inference-lifecycle | in sync | `1f5bc91` | `4bdd9b2`。スレッド版の対象は不変 | 合法1,850列、実スレッド4ケースPASS、6破壊検出 |
| inference-startup | updated | `4bdd9b2` | `e97e068`。再利用する検証器の同期値のみ | 60列、実プロセス8ケースPASS、4種の破壊検出・復元PASS |
| lesson-endurance | updated | `1f5bc91` | `4bdd9b2`、`e97e068`。準備期限の引数には既に追従済み | 期限6ケース、3000仮想秒8構成、実プロセス4ケースPASS、5破壊検出・復元PASS |
| lesson-liveness | in sync | `1f5bc91` | `4bdd9b2`、`e97e068`。注入するスレッド版の契約は不変 | 合法413列、固定6ケースPASS、6破壊検出 |
| lesson-recording | in sync | `5381edb` | `1f5bc91`、`4bdd9b2`。記録器・授業の制御は不変 | 通常1,555列、ASR待機8順序、保存障害4ケースPASS、3破壊検出 |
| school-access | in sync | `5381edb` | `1f5bc91`、`4bdd9b2`。認可・制限は不変 | 送信元3,703、公開10,626、参加4,891状態・28,586遷移、期限境界960、WS13、リスナー6要求PASS、30破壊検出 |
| teacher-audio | updated | `e97e068` | `421edec`、`0710fa3`、`4efcb9f`。復旧が実装され、旧FAIL記述が残った | 合法24列、30,000フレーム、英中各600字幕PASS、7破壊検出・復元PASS。Edge全3ケースPASS |
| teacher-lifecycle | in sync | `5381edb` | `1f5bc91`、`4bdd9b2`。所有権・自動停止理由は不変 | 合法668列PASS、6破壊検出 |
| turn-assembly | in sync | `5381edb` | `1f5bc91`、`4bdd9b2`。Turn本体と設定は不変 | 30,941列・4設定の123,764ケースPASS、12破壊検出 |
| vad-segmentation | in sync | `5381edb` | `1f5bc91`。feed・flush契約は不変 | 129,768ケースPASS、6破壊検出 |
| ws-teacher-ingress | in sync | `5381edb` | `1f5bc91`、`4bdd9b2`。受信と先生認可は不変 | 認可260ケースと参加制限拡張PASS、既存7件と参加制限13件の破壊検出 |

更新前も、耐久検証以外の13本は終了コード0だった。先生音声は既存5破壊を含む更新前実行で24合法列すべてPASSとなり、古い基準の20成功・4失敗と異なった。これは古い記述のまま偽の成功を返したケースではなく、実コードを直接実行する検証器が製品修正を観測したものだった。

## 検証側の更新

先生音声では、Contextの再開と終了trackの再取得をそれぞれ無効化する2破壊を既存の表に加えた。`start, pause, suspend, start` と `start, pause, end_track, start` で、新しいPCMが届かず `capture_liveness` 違反になる。復元後は24列すべて成功する。元の製品ファイルは変更せず一時コピーで実行した。

この性質は変更していない。旧不具合は `523087dc1b665a6342e10f51ddcdc57c822be1f1` の `micReady` だけを見た早期returnに由来し、`0710fa3` で修正された。実Edgeでも通常、Context休止、track終了の全ケースで、再開後の新しい音声と英中各1件以上の字幕増分を確認した。古い基準結果は `421edec:scripts/formal/teacher_audio_baseline.json` と同コミットのブラウザー基準結果で参照できる。

耐久検証の更新前実行では `ValueError: invalid literal for int() with base 10: ''` が出た。子が正式名でPIDファイルを開き、親が存在を確認し、子の書込み前に親が空内容を整数へ変換する順序だった。導入は `1f5bc91`。準備検証にも `e6cf7f9` から同じ通知方法がある。両方を一時名へ書いて閉じてから正式名へ置き換えるよう修正した。

`test_process_markers.py` は書込み直前でwriterを停止し、正式名がまだ見えないことを要求する。旧実装は2件とも `published incomplete PID: ''` で失敗し、修正後は2件とも成功した。公開後に完全なPIDが読めることも確認する。読み取り側の再試行や探索上限の緩和は加えていない。

caption-recoveryの対応表を実際の `CaptionDelivery.confirm`・`publish` 呼出しへ直し、破壊内容と7件という現行の件数を記した。READMEの修正済み反例を現在の不具合とする記述も直した。teacher-lifecycleとturn-assemblyでは、過去の基準結果と現在の同期先を区別した。

## 再実行の条件

Windows、Python 3.12.10、Node.js 24.22.1、固定依存、Playwright 1.64.1とインストール済みEdgeを使用した。sandbox内のasyncio初期化が停止した初回試行は中断し、通常のローカル通信が許可された環境で再実行した。環境の停止を製品の反例には数えていない。

各 `.agents/skills/<検証名>/SKILL.md` の主コマンドを、記載された既存 `.venv/Scripts/python.exe` で実行した。全14本で `--mutation-check` を指定し、ws-teacher-ingressには `--join-limit-check` も指定した。teacher-audio-browserは別コマンドで実行した。追加の回帰検査は次のコマンドで実行できる。

```powershell
.venv/Scripts/python.exe -X utf8 -B -m pytest scripts/formal/test_process_markers.py -q
```

スキルと検証器の同期先は確認した製品コミットへそろえた。過去の反例として明示されている記録・字幕復元・先生交代のbaselineは保存した。推論・継続・耐久・先生音声の現行baselineは今回の出力で更新した。基のブランチのみを取得する新規bare cloneでも、同期SHAの解決と対象ファイルの `git diff` が成功した。

## 残る範囲外

すべて有限入力と指定したスケジュールの検査であり、無制限の証明ではない。上限は一つも減らしていない。

| 検証 | 保証しない範囲 |
|---|---|
| asr-backlog | ASR計算、複数ワーカー、メモリー総量 |
| caption-recovery | 無限の言語変更・再接続、全DOM描画、履歴失効 |
| client-delivery | 実LAN、全送信スケジュール |
| inference-lifecycle | プロセス版、3要求以上、全スレッド順序 |
| inference-startup | 実モデル、全OS順序、同期のprocess.start自体の中断 |
| lesson-endurance | 実時間の50分性能、実モデル精度、LAN、DOM |
| lesson-liveness | 実モデル32反復、LAN、全asyncio順序 |
| lesson-recording | 先生画面の終了ボタンからの保存再試行、複数未完了ASR、実ディスク破損 |
| school-access | 全IP・全時刻・実学校LAN |
| teacher-audio | マイク取得拒否、既定マイクへの切替、選択競合、未完了Promiseの全順序、物理マイク |
| teacher-lifecycle | 3先生を超える並行参加、任意の並行control |
| turn-assembly | 代表語尾以外の言語的正しさ、無限長の認識断片 |
| vad-segmentation | Silero判定、全入力長、実音声での認識品質 |
| ws-teacher-ingress | 全入力解析、任意のWebSocket順序 |

新たなマイク復旧の取得拒否・選択競合は通常の先生画面テストに固定ケースがあるが、今回の有限操作列には含めていない。#192の保存再試行は既存の記録検証が直接呼ぶサーバー契約の前段にある。今回、その画面操作の保証を追加したとは扱わない。

モデル・表示処理・性能の変更はないため、条件付きの実モデル32反復、生徒DOMへのフレーム再生、実時間50分の性能試験は再実行していない。過去の性能測定値も変更していない。

## 判断に使った原則

`Model the Domain` に従い、先生音声の破壊は既存の `MUTATIONS` 表へ2件加えた。`Fix Root Causes` に従い、空PIDを読み直す処理を加えず、書き込み側で完成前の公開を防いだ。`Build the Lever` に従い、競合を決定的に再現する `test_process_markers.py` を残した。`Prove It Works` に従い、実コードへの故障注入と実Edgeの音声・字幕到達を確認した。
