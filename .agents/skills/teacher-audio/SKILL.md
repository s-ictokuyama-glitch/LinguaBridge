---
name: teacher-audio
description: 先生ページの開始・一時停止・音声処理休止・マイク終了を有限操作列で検査し、実Workletの50分PCMを英中字幕まで通す。Use when changing teacher.js microphone startup/resume or send gating, audio-worklet.js conversion, or investigating captions stopping after capture interruption.
---

# 先生の音声入力と再開を確認する

マイクを利用できる状態で先生が「開始」「再開」を押したら、新しい音声を取り込み、翻訳字幕へ届ける。一時停止中は音声を送信しない。
違反すると、先生画面は「配信中」でも生徒の字幕が更新されない。

既存の [lesson-endurance](../lesson-endurance/SKILL.md) は `Pipeline.feed_audio` にPCMが届くことを前提とする。このスキルは前段の `ensureMic`、実AudioWorklet、先生側の送信処理を検査する。

## 全探索と50分入力を実行する

リポジトリ直下でNode.js 24.22.1、Python 3.12.10と固定依存を使う。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/teacher_audio.py --mutation-check --json .audit/teacher-audio.json
```

同じ依存がある `.venv/Scripts/python.exe` も使える。Windowsの制限環境でasyncioの内部socketpair生成が停止した場合は、実行権限を確認して再実行する。環境の停止を製品の反例として扱わない。

終了コードとJSONの `status` を両方読む。0は全性質の成功、1は性質の違反、2は検証器・環境の失敗。終了コード1を期待するテストとして包んで成功扱いにしない。

[基準結果](../../../scripts/formal/teacher_audio_baseline.json) は総合 `FAIL`。`capture.bounded` は85候補、合法24列、成功20列、違反4列。`capture.sustained` と `translation` は `PASS`、30,000フレーム、英中各600字幕を確認する。
`mutations.results` は5件すべて `detected: true`。正常対照が `PASS` で、指定した性質の違反を検出したことまで確認する。`restored_bounded_matches: true` と `restored_translation: PASS` は元の既知反例集合と正常入力へ戻ったことを示す。

`--seconds 5` と `--case initial|paused|resume_suspended|resume_ended` は短い切分け用。`--case` には列挙した名前を1つ渡す。最終確認には既定の全探索・3000秒入力を使う。

## 実ブラウザーで反例を照合する

Media APIの代役や復旧処理を変更した場合は、インストール済みEdgeとPlaywright 1.64.1で実行する。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/school_access_requirements.txt --with playwright==1.64.1 --with pytest==9.1.1 --with websockets==17.1 python -X utf8 -B scripts/formal/teacher_audio_browser.py --json .audit/teacher-audio-browser.json
```

通常のアプリ依存と同じPlaywrightがある `.venv/Scripts/python.exe` も使える。
実getUserMedia、MediaStream、AudioContext、AudioWorklet、ループバックHTTP/WS、授業と字幕処理を通す。機器とASR・MT計算は代役。物理マイクを使用しない。
[ブラウザー基準結果](../../../scripts/formal/teacher_audio_browser_baseline.json) は通常対照 `PASS`、`suspend` と `ended` が `FAIL`、終了コード1。再開後1.5秒の音声と、最後のpauseから1秒後の英中字幕増分を読む。通常対照が失敗した場合は `ERROR` として環境・検証器を調べる。

## 反例を判断する

現行実装では次の順序が失敗する。

1. 開始して音声と字幕が届くことを確認する。
2. 一時停止する。Contextを `suspend()` するか、現在のtrackを `stop()` する。
3. 再開ボタンを押す。`ensureMic` が `micReady` だけで戻り、開始制御は送信される。
4. Context休止ではPCMが止まる。track終了では無音PCMが届き続ける。両方とも新しい英中字幕は届かない。

有界探索では故障とpauseを逆順にした2列も失敗する。自動復旧は要求せず、先生が再開を押した後を判定する。送信件数だけの確認はtrack終了を見逃すため、新しいPCM値を比較する。
実コードの反例は保持する。製品修正は依頼された場合に行い、修正後は同じ性質のまま全列が成功することを確認する。

## 実装との同期を確認する

同期コミットは `e97e06890846bb48de7b5f16dfa65bd8cbb86a77`。

| 対象 | 照合する契約 |
|---|---|
| `web/teacher.js` | `init` のクリック、`ensureMic`、`attachStream`、Workletからの送信、`connect`、`applySessionState`、`setButtons` |
| `web/audio-worklet.js` | `PCM16Downsampler.process` の48kHz→16kHz変換と1600サンプル送信 |
| `server/connection.py`、`server/lesson.py` | 参加、開始、音声を実Pipelineへ渡す処理 |
| `server/pipeline.py`、`server/caption_delivery.py` | PCM受理、ASR、Turn確定、翻訳、先生と英中生徒への配信、停止 |
| `server/audio`、`server/asr_backlog.py`、`server/inference.py`、`server/turn` | energy VAD、simple Turn、制御Executorによる完了と待ち行列 |
| `server/roster.py`、`server/delivery.py`、`server/caption_history.py`、`server/mt/cache.py`、`server/config.py` | 受信者、送信、履歴、キャッシュ、設定 |
| `scripts/formal/lesson_liveness.py`、`scripts/formal/lesson_endurance.py` | 再利用するScenario、決定的計算、Clock、TimedExecutor、callback処理 |
| `scripts/qa_native_browser.py`、`scripts/qa_browser.py`、`tests/conftest.py`、`tests/helpers.py`、`server/main.py`、`web/student.js` | 別コマンドの実サーバー起動・合成機器・字幕観測 |

```powershell
git diff e97e06890846bb48de7b5f16dfa65bd8cbb86a77 -- web/teacher.js web/audio-worklet.js server scripts/formal/lesson_liveness.py scripts/formal/lesson_endurance.py scripts/qa_native_browser.py scripts/qa_browser.py tests/conftest.py tests/helpers.py web/student.js
```

メディア状態・クリック処理・await境界が変わったら操作の合法性と代役を照合する。実ブラウザーとの一致、全探索、正常50分入力、破壊検出と復元を確認した後だけ、この同期コミット、Pythonの `SYNC_COMMIT`、基準結果を更新する。同期先は検証した対象ファイルと同じ内容を持つ基のブランチの履歴から選ぶ。未確認の変更が溜まった場合は `/maintain-formal-verification-skill` を使う。

## 保証範囲を守る

Node VMで実JSへの有限入力を全列挙する。別言語へ実装を写すモデルより、実コードの早期returnを直接検査する方法を選んだ。SMT・無制限の証明ではない。
初回開始を固定し、pause・start・suspend・end_trackの長さ0〜3を全列挙する。pauseはlive、startはpaused、suspendはrunning、end_trackはlive trackに限る。クリック後は200msの新しい値の音声で判定する。DOM・Media API・WS応答は決定的な代役で、Promise完了順序の全探索ではない。
50分ケースは48kHz・128サンプル入力、4秒の声と1秒の無音、600発話を実Workletと先生の送信処理に渡す。全PCM値を独立した入力計画と比較し、実Connectionから英中字幕まで通す。ASR 0.1仮想秒、言語ごとのMT 0.2仮想秒、100msごとの入力と各16周回のcallback処理、末尾10仮想秒の排出を使用する。発話ごとのdrain待ちは行わない。
計算はIDを返す代役、VADはenergy、Turnはsimple、partialは無効。正常50分ケースと障害後の短い再開確認は別の保証。実時間の50分性能、物理機器の抜去・OSスリープの発生条件、実モデル精度、LAN、全ブラウザー、送信buffer容量、マイク切替の競合、新しい先生タブ、既定morphの網羅は対象外。
