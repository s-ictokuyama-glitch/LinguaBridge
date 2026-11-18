---
name: asr-overload-recovery
description: 先生の音声が ASR の秒数上限を超えたとき、破棄の通知、受理済みの英中字幕、容量が空いた後の翻訳再開を PCM 入力から有界検証する。AsrBacklog の容量、Pipeline の過負荷処理、morph の無音境界を変更するときや、過負荷後も生徒の字幕が止まるときに使う。
---

# 音声認識の待ちが満杯になった後の字幕を検証する

授業中に ASR が遅れても、上限内で受け取った音声は順に一度ずつ翻訳され、空いた容量には次の音声が入る。超過分を捨てたときは先生に通知する。この契約が壊れると、先生が話し続けても生徒には途中から訳文が届かない。

[asr-backlog](../asr-backlog/SKILL.md) は待ち行列単体の受理・排出を扱い、[lesson-liveness](../lesson-liveness/SKILL.md) の授業検査は音声破棄0件を前提とする。ここでは ASR を意図的に遅らせ、実際に届いた確定原文と英中の字幕まで調べる。

## 実行する

リポジトリ直下から Python 3.12.10 と[固定依存](../../../scripts/formal/requirements.txt)で実行する。モデルのダウンロードは不要。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/asr_overload_recovery.py --mutation-check --json .audit/asr-overload-recovery.json
```

同じ依存を持つ Windows の開発環境では `.venv/Scripts/python.exe -X utf8 -B scripts/formal/asr_overload_recovery.py --mutation-check` でも実行できる。同期時点の結果は全129ケース成功、実際に破棄が起きたのは78ケース。破壊検査の各項目に期待した反例があり、`restored_matches` が `true` になることを確認する。

## 何を全列挙するか

Python で有限の PCM 入力列と認識完了順を作り、製品の非同期ワーカーに与える。Segment の受理、morph の確定、配信を同じ実装の中で追え、別の状態機械への転記を省ける。

- **上限3秒の128通り**。1秒か2秒の Segment を4件入力し、3つの発話間隔それぞれで「最古の ASR を1件完了させる」「まだ完了させない」を全列挙する。各列の後で処理を排出し、新しい音声をもう1件送る。
- **出荷上限30秒の1通り**。10秒の A、B、C を保持し、D の入力後に A の認識だけを完了させる。続けて E、F を送り、排出後に新しい G を送る。期待する原文と各言語の字幕は A、B、C、E、G の5件。D、F の計20秒を捨て、先生に2回通知する。E はまだ B、C が滞留している時点で受理される。

各入力は16 kHz の PCM16 を100 msずつ渡す。energy VAD では200 msのプリロール、有音、末尾500 msの無音が ASR 容量に数えられる。例えば1秒の Segment は有音300 msから作る。発話ごとに合計1秒の無音を置く。ASR の代役は波形の識別子を「条件1が異なるので」のような継続語尾に戻すため、morph は後続の無音境界を処理して初めて原文を確定できる。

期待値は入力した識別子、予定した Segment 長、明示的に完了させた認識から計算する。実装が返す破棄件数を使って期待字幕を減らさない。実際に ASR に渡った音声の識別子・長さ、破棄件数・秒数、10秒の同種通知抑制、先生と両言語の受信順・発話番号・原文・訳文を照合する。

## 反例を読む

`PASS` と終了コード0が合格。`FAIL` と終了コード1では `first_counterexamples` の `steps` で再現し、最初に異なる `expected` と `actual` を読む。30秒ケースだけなら `--shipping`、3秒ケースなら例えば `--case "1,2,1,release,1"` を使う。

`dropped_audio` は想定した破棄件数・秒数、`asr_input` は次に認識すべき入力とサンプル数、`teacher_notice` は通知回数、`drained_audio` は排出後の占有秒数を示す。`teacher_finals` が合っていて `captions_en` または `captions_zh` が異なる場合は、該当言語の翻訳・送信経路を調べる。

`--mutation-check` は一時コピーで実装を壊し、正常だった30秒ケースで意図した反例が出ることを確認する。例えば処理中の A を容量から除くと D まで受理され、期待した破棄1件に対して実際は0件になる。回復後の中国語だけを落とす破壊では、中国語の G が欠ける。その後、元の実装で全探索を再実行して結果を照合する。

`ERROR` と終了コード2は検査未完了を示す。起動環境、タイムアウト時のワーカー、破壊置換箇所を確認する。Windows で asyncio の初期化から出力がない場合は、ローカルのソケットが利用できる環境で再実行する。入力と実装の対応を確認したうえで再現する反例は残し、製品修正は別途扱う。

## 実モデルと日本語音声で照合する

VAD やモデルの切替後、または実際の音声でだけ翻訳が再開しない場合は、通常のアプリ依存と出荷モデルを用意して次も実行する。音源がない Windows 環境では先に `./scripts/make_fixture_audio.ps1 -BaseOnly` で日本語のテスト音声を生成する。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/asr_overload_real.py --json .audit/asr-overload-real.json
```

モデルの場所は `config.yaml` の `models.dir` を使い、別の場所は `--models-dir` で指定する。対照では日本語の WAV を1本ずつ排出する。過負荷側では同じ出荷設定で、中央の12本を ASR ワーカーへ実行を戻す前にまとめて入力する。前後に別の音声を入れ、対照と同じ原文・英訳・中国語訳が届くことを照合する。末尾の新音声では実際の MT 呼出しも確認する。同じ文が続く区間では製品の翻訳キャッシュが有効。

実モデル実行の終了コード0と `status: PASS` を確認する。同期時点では、対照の各 Segment は67,072サンプルだった。30秒枠に先着7件（29.344秒）が入り、続く5件（20.96秒）は破棄されて先生に通知が1件届いた。排出後の別音源は2件の原文に分かれ、英中の実訳が各2件とも対照と一致した。音声やモデルを更新して同じ分割にならないときは、対照の `offers` と発話ごとの確定原文から調べる。この固定入力例は無制限の授業や実時間の処理性能を保証しない。

## 実装との同期

同期コミットは `11deec4a2877913d210351e83b57d6134ca20858`。

| 対象 | 変更時に照合する処理 |
|---|---|
| `config.yaml`、`server/config.py`、`server/audio/{ingest,vad}.py` | 出荷上限、morph の開始・終了条件、PCM変換、プリロールと `Segment` のサンプル数 |
| `server/asr_backlog.py`、`server/inference.py` | `offer`、`next`、`join`、`InferenceRunner.run` の容量解放点 |
| `server/pipeline.py` | `feed_audio`、`_enqueue_asr`、`_notify_overload`、`_asr_worker`、`_close_pending_turn`、`_publish_turn`、`drain` |
| `server/turn/`、`server/asr/hallucination.py` | 認識断片の採否、継続語尾と無音による確定 |
| `server/caption_delivery.py`、`server/caption_history.py`、`server/mt/cache.py`、`server/{roster,delivery,ws_protocol}.py` | 採番、ライブ翻訳ジョブ、言語別送信とフレーム |
| `scripts/formal/lesson_liveness.py` | 使用する `ControlledExecutor`、`Socket`、`Translation`、callback の進め方 |
| `server/main.py`、`server/inference_process.py`、`server/asr/haas_onnx_engine.py`、`server/mt/hymt_engine.py`、`scripts/formal/{lesson_liveness_real,lesson_endurance}.py` | 実モデル照合の `create_app`、推論プロセス、受信Socketと子プロセス終了判定 |

```powershell
git diff 11deec4a2877913d210351e83b57d6134ca20858 -- config.yaml server scripts/formal/lesson_liveness.py scripts/formal/lesson_liveness_real.py scripts/formal/lesson_endurance.py scripts/make_fixture_audio.ps1
```

入力粒度、ASR が容量を解放する時点、通知抑制、Turn 境界、字幕形式の変更を確認する。意図した契約変更なら根拠とともに入力台帳と破壊箇所を更新する。実装との対応、全探索、破壊の検出、復元後の一致を確認してから同期コミットを進める。未検証の変更が重なった場合は `/maintain-formal-verification-skill` を使う。

有界探索では ASR と MT の計算結果を固定し、認識の完了だけを手動で進める。MT の容量は出荷値256件で、探索中に満杯にならない。物理マイク、ネットワーク、ブラウザー表示、実モデルの意味精度、実時間50分の速度や途切れの頻度は保証範囲外。
