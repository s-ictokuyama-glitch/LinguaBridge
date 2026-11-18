---
name: haas-decode
description: 既定Haas ONNXの認識ループの前進、発話間の状態分離、長音声のPCM保持、英中字幕への継続を実コードの有界探索で検証する。Use when changing HaasOnnxEngine, _HaasDecoder decoding/prediction, internal chunking, or investigating ASR stalls despite completed ONNX calls.
---

# Haasの音声認識を終えて次の字幕へ進む

有限の音声を認識し終え、次の音声も翻訳字幕まで処理できることを検査する。
違反すると、先生の発話の認識が終わらないか、音声が欠落して後続の字幕が滞る。

## 検証する

リポジトリ直下でPython 3.12.10と固定依存を使う。モデルのダウンロードは不要。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/haas_decode.py --mutation-check --json .audit/haas-decode.json
```

同じ依存が入った `.venv/Scripts/python.exe` でも実行できる。
Windowsの制限環境でasyncioの初期化が停止する場合は、実行権限を確認して同じコマンドを再実行する。
終了コード0、`status: PASS`、終端列2,973、PCM分割33ケース、13発話から英中各9字幕、12破壊の `detected: true`、`restored_matches: true` を確認する。
基準結果は [haas_decode_baseline.json](../../../scripts/formal/haas_decode_baseline.json) にある。

失敗したら `property`、`trace`、`expected`、`actual` を [検証範囲](../../../scripts/formal/haas_decode.md) と製品コードに照合する。
`--probe decode`、`witness`、`chunks`、`classroom` は部分再現用。最終確認には全検証を使う。
`FAIL` の実際の反例は残し、成功のために範囲や期待値を弱めない。製品コードの修正は依頼された場合に行う。
`ERROR`、Traceback、子プロセスの期限超過、破壊の置換不一致は検証器か環境の問題として調べる。

## コードとの対応を確認する

同期コミットは `d595f375722d2190746fdbdb3b34de0aacc4d6e8`。

| 対象 | 照合する処理 |
|---|---|
| `server/asr/haas_onnx_engine.py` | `_chunks`、`_predict`、`_decode`、`_text`、`_features`、`HaasOnnxEngine.transcribe`。manifest固定値、初期化される設定値も読む |
| `server/pipeline.py`、`server/audio/ingest.py`、`server/audio/vad.py` | PCM受理、energy分割、ASR処理、Turn確定、排出 |
| `server/asr/base.py`、`server/asr/hallucination.py`、`server/asr_backlog.py`、`server/inference.py` | 認識結果、空認識の除外、待ち行列と推論完了 |
| `server/turn/assembler.py`、`server/turn/boundary.py` | morphの確定条件 |
| `server/caption_delivery.py`、`server/caption_history.py`、`server/mt/cache.py` | 翻訳、採番、原文と訳文の対応 |
| `server/roster.py`、`server/delivery.py`、`server/ws_protocol.py` | 英中の受信者と送信キュー、字幕形式 |
| `server/config.py`、`config.yaml` | 既定ASR、morph、容量、推論期限 |
| `scripts/formal/lesson_liveness.py` | 制御Executor、Socket、決定的MT、診断、callback処理 |

```powershell
git diff d595f375722d2190746fdbdb3b34de0aacc4d6e8 -- server config.yaml scripts/formal/lesson_liveness.py
```

対象の分岐・配列・await境界が変わったら、入力台帳、代役の契約、観測点と破壊箇所を見直す。
モデル設定やmanifestを変更したら `common/decoding.json` のblank・duration・上限とtokenの対応を照合する。
検証器はロードを省略し、確認済みの設定値を固定している。
全検証、破壊の意図した失敗、復元後の一致を確認してから、スキルと検証器の `SYNC_COMMIT`、基準結果を更新する。
同期先には対象コードと同じ内容を持つ基のブランチのコミットを使う。
未確認の変更が溜まった場合は `/maintain-formal-verification-skill` を使う。

## 保証の限界を守る

実Pythonコードに対する有限入力の全列挙であり、無制限の形式証明ではない。
全列挙はencoded frame数0〜2、代表token1種類とblank、duration 0〜4、上限10。
これ以外の語彙・音声長・波形、ONNXの内部計算や停止、モデルのロード、認識・翻訳精度は保証しない。
長PCM分割の検査は内部decoderを代役にする。字幕の結合は短PCM13発話、energy VADとmorphを使う。
ONNX入出力とMTは代役、frontendとprediction状態は縮小した合成設定。
encoderのパディング境界、マイク、実時間の50分性能、実ネットワーク、生徒画面は対象外。

ONNX呼出し自体の停止と回収は [lesson-endurance](../lesson-endurance/SKILL.md)、
既定Sileroと長い連続発話は [continuous-speech](../continuous-speech/SKILL.md)、
実モデルでの反復と生徒画面は [lesson-liveness](../lesson-liveness/SKILL.md) を併用する。
