---
name: teacher-page-rejoin
description: 先生ページの再読み込み、別タブへの交代、同じタブでの通信復帰後に、マイクから英中の字幕表示まで再開するかを実ブラウザーで有界検証する。teacher.js の再参加・マイク取得・開始ボタン、Lesson の自動再開を変更するときや、配信中表示のまま字幕が止まるときに使う。
---

# 先生の再参加後に授業の字幕が続くか確かめる

配信中の先生が再参加したら、続く2発話が英語・中国語の生徒画面に順に1回ずつ届く。手動で一時停止した授業では、再参加しても停止を保ち、「再開」を押してから音声を送る。ここが壊れると、生徒の字幕だけが以前の説明のまま止まり、先生画面は「配信中」を示し続ける。

[teacher-continuity](../teacher-continuity/SKILL.md) はサーバーで受理したPCMから検査を始める。[teacher-microphone](../teacher-microphone/SKILL.md) は初回取得済みの画面での操作競合を扱う。新しいタブのマイク未取得は[既存のブラウザーQA](../../../docs/qa/README.md)にも記録がある。このスキルはマイクを取り直す必要の有無と再参加前の授業状態を組み合わせ、先生が実際に送った音と生徒が読むカードを照合する。

## 全探索を実行する

Windows の Microsoft Edge と Python 3.12.10 を使う。リポジトリ直下で次を実行する。検証時は Edge 158.0.4370.33、Playwright 1.64.1。結果の `browser` に実行時の版を記録する。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/teacher_page_rejoin_requirements.txt python -X utf8 -B scripts/formal/teacher_page_rejoin.py --mutation-check --json .audit/teacher-page-rejoin.json
```

同じ依存を持つ開発環境では先頭を `.venv/Scripts/python.exe` に置き換えて実行できる。日常の全10通りの検査は `--mutation-check` を外す。配信中の再読み込みだけを試すには次を実行する。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/teacher_page_rejoin.py --case reload live running
```

## 入力と観測

全ケースで先生と英中各1人の生徒を実際のページから参加させる。まず先生が「開始」を押し、1発話目の字幕が両方に届くことを対照として確認する。次に、下表の条件と授業状態（配信中・手動一時停止）の直積を全列挙する。

| 再参加 | 操作 | 接続し直す直前のマイク |
|---|---|---|
| `socket` | 同じページのWebSocketを閉じ、製品のタイマーで再接続する | 正常、AudioContextを休止、取得済みtrackを終了 |
| `reload` | 同じタブを再読み込みする | 正常。前のドキュメントのマイクは引き継げない |
| `takeover` | 同じブラウザーで2枚目の先生タブを開く | 旧ページのマイクは正常。旧接続の終了コード4000も確認する |

再参加後、手動停止のケースでは再開前に1発話を再生し、送信パケットも字幕の追加も0であることを確認する。配信中に入力の再取得が必要なときは「マイクを再開」を1回押す。再開操作の後は AudioContext が running、入力トラックが live になり、新しい音声パケットが送られるのを待つ。続けて異なる2発話を入力し、各発話の後で英中双方のカード全件を比較する。

検証器は Playwright で Edge を操作し、Web Audio の音源を `getUserMedia` が返す MediaStream にする。音源はマイク未取得のときも同じ長さだけ再生する。製品の AudioWorklet、100 ms のPCMパケット、ループバックHTTP・WebSocket、音声区間化、morph、翻訳キュー、配信、DOMへの描画を通る。ブラウザーのAPI・授業状態・音声処理を手書きの状態機械へ写さずに、境界で起きる欠落を直接観測できる。

高速探索では、400・800・1,200 Hz の各400 msを使う。最初の対照は音声パケットを3件観測した後に再生する。各発話は250 ms後に再生を開始し、末尾に1.4秒の無音を置く。energy VADを使い、認識の代役が受信したPCMの周波数から原文を決める。翻訳の代役は選択言語を明示した文字列を返す。期待するカード列は入力計画から作る。各発話の後は先生ソケットの応答、ASRとMTの排出、生徒ソケットに積む観測用メッセージの順に待ってからDOMを読む。発話を強制確定するための一時停止は行わない。

## 反例を読む

終了0は `PASS`、1は `FAIL`、2は `ERROR`。既知の欠落が再現したときも1を返す。`cases[].rejoined` で表示・AudioContext・track・開始ボタンを、`phases` で次の2発話の送信数・有音パケット数とカードの番号・原文・言語・訳を読む。

- `rejoin_state` は参加通知・交代通知・授業状態の不一致、`resume_state` は再開後も授業が配信中にならないことを示す。`resume_input` は再開操作後に音声入力の準備を確認できなかったことを示す。
- `paused_quiet` は手動停止中の送信または字幕の追加を示す。`first_captions` と `following_captions` は各発話後のカード列の不一致を示す。
- 音声パケットが0なら、再参加時のマイク取得と開始ボタンを調べる。有音パケットがあるなら、認識、翻訳、配信、描画の順に追う。最初の正常対照を作れなかった場合やブラウザー・モデルの起動失敗は `ERROR` になる。反例と区別して、入力、依存、モデル、loopbackの待受けを確認する。

Issue #221 の修正前（`f1188b2`）は、配信中の再読み込み、別タブへの交代、音声処理が休止またはトラックが終了した同じタブの再接続で、次の2発話が欠けた。修正後は全10条件で英中双方に2発話が順番どおり1回ずつ届いた。マイクが正常な同じタブの通信復帰には操作が要らない。手動停止した5条件では「再開」を押すまで送信は0件だった。

`--mutation-check` は `web/` の一時コピーを実際に配信する。再開ボタンを無効にする、3枚目の中国語カードを落とす、手動停止を勝手に再開する、停止中もPCMを送るという4変更を、正常対照・破壊・復元の順に照合する。最後の全探索で各ケースの違反が元に戻ることを `restored_matches` で確認する。

## 日本語音声と出荷モデルで再現する

Silero、Haas ONNX int8、Hy-MT2 と Playwright が入った開発環境を使う。日本語教材 `tests/fixtures/ja/01.wav` 〜 `03.wav` がなければ `./scripts/make_fixture_audio.ps1 -BaseOnly` で生成する。モデルの場所は `--models-dir` で指定できる。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/teacher_page_rejoin.py --real --case reload paused running --json .audit/rejoin-control.json
.venv/Scripts/python.exe -X utf8 -B scripts/formal/teacher_page_rejoin.py --real --case reload live running --json .audit/rejoin-live.json
```

配信中の再読み込みでは「マイクを再開」、手動停止後の再読み込みでは「再開」を押す。両方のケースで「光合成には日光と水と二酸化炭素が必要です」「教科書の42ページを開いてください」の英訳・中訳が字幕の2枚目、3枚目に表示された。同じページで通信だけを切る対照には `--real --case socket live running` を使う。実モデルでは翻訳キャッシュを無効にし、教材ごとの日本語の目印、発話番号、宛先言語、空でない訳、両画面の原文の一致を調べる。訳文の品質、物理マイク、学校LAN、授業50分間の性能は別の評価を要する。

## 実装との同期

下のコマンドは、検証済みの `web/teacher.js` を追加したコミットを現在の履歴から探す。squash マージ後も同じファイル内容を使って差分を確認できる。

| 対象 | 照合点 |
|---|---|
| `web/teacher.js` の `connect`、`handleMessage`、`applySessionState`、`setButtons`、`ensureMic`、`replaceAudioWorklet` と `web/audio-worklet.js` の `process` | 再参加の通知順、マイクを取得する操作、送信される有音PCM |
| `server/main.py` の `create_app`、`server/connection.py` の `handling`・`receive`、`server/lesson.py` の `join`・`leave`・`control`、`server/roster.py` の `add` | 実WSの切断と先生交代、手動停止・自動再開 |
| `server/pipeline.py` の `feed_audio`・`drain`、`server/caption_delivery.py` の `publish`・`_mt_worker`、`web/student.js` の `handleMessage`・`showCaption` | 発話確定、英中への配信、各生徒のカード順・内容 |
| `config.yaml`、`scripts/formal/teacher_page_rejoin.py`、`teacher_rejoin_media.js` | 出荷構成、操作集合、仮想マイクの再生完了と観測位置 |

```powershell
$rejoinBaseline = git log --reverse --format=%H --find-object=64ea0a1aa4780d1a2088e6aac497613ac3aca958 -- web/teacher.js | Select-Object -First 1
git diff $rejoinBaseline -- config.yaml web server
```

再参加の通知やボタン操作が変わったら、入力を送れる時点と有効な操作を製品コードで確かめる。意図した契約変更は操作・期待値・破壊検出に反映する。10通りと破壊検出・復元、実モデルの対照と反例を再照合してから同期コミットを進める。未照合の変更が溜まったら `/maintain-formal-verification-skill` を使う。

有界探索の範囲は先生の再参加1回、固定した英中の生徒各1人、成功するマイク許可、固定した3発話。通信障害の全順序、再参加の反復、ブラウザーの自動再生制限による拒否、モデル推論の全入力は保証範囲の外にある。
