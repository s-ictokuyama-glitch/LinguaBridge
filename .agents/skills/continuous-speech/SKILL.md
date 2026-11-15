---
name: continuous-speech
description: 既定Sileroで30秒を超えて話し続けたときのPCM持越し、morph確定、英中字幕の継続を実コードの有界入力探索と50分仮想入力で検証する。Use when changing VoiceSegmenter forced splits/carry, Pipeline segment handling, morph time limits, or investigating captions stopping during continuous speech.
---

# 長い連続発話を翻訳し続ける

無音を挟まずに話し続けても、強制分割したPCMを欠落・重複なく認識へ渡し、英中字幕を更新し続ける。
違反すると、先生が長く説明している間に音声が失われるか、生徒の字幕が止まる。

## 検証を実行する

リポジトリ直下でPython 3.12.10と固定依存を使って実行する。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/continuous_speech.py --mutation-check --json .audit/continuous-speech.json
```

同じ依存が入っている場合は `.venv/Scripts/python.exe` でも実行できる。
Windowsの制限環境でasyncioの初期化が止まる場合は、実行権限を確認して同じコマンドを再実行する。
終了コード0と `status: PASS` を確認する。有限162ケースがすべて通り、50分入力、9種類の破壊検出、復元後の結果一致が成功することを確認する。
[Issue #202 の再現条件](../../../scripts/formal/continuous_speech.md#issue-202-の再現条件) も162ケースに含まれる。[基準結果](../../../scripts/formal/continuous_speech_baseline.json) は検証器追加時の記録で、修正前の18ケースの失敗を含む。

`FAIL` は `property`、`trace`、`expected`、`actual` を製品コードと照合する。
実際の反例は残し、成功させるために入力範囲や期待値を弱めない。
`ERROR`、Traceback、子プロセスの期限超過、破壊箇所の置換不一致は検証器か実行環境の問題として調べる。
製品コードの修正は依頼された場合に行う。

## 実装との同期を確認する

最終同期コミットは `ea925f9ed3be791d96e24faa975a00ec4c81d99c`。

| 対象 | 照合する処理 |
|---|---|
| `server/audio/vad.py` | SileroVAD、VoiceSegmenter.feed、_maybe_force_close、持越しPCMと時刻、flush |
| `server/audio/ingest.py`、`server/pipeline.py` | PCM変換、feed_audio、_asr_worker、_process_segment、_publish_turn、flush_audio、drain、stop |
| `server/asr_backlog.py`、`server/inference.py` | 入力容量、ASR順序、制御Futureの実行と完了 |
| `server/turn/assembler.py`、`server/turn/boundary.py` | 継続語尾、時刻によるTurn上限、末尾確定 |
| `server/caption_delivery.py`、`server/caption_history.py`、`server/mt/cache.py` | 翻訳投入、採番、英中配信、保持量 |
| `server/roster.py`、`server/delivery.py`、`server/ws_protocol.py` | 言語別の送信、字幕形式と順序 |
| `server/config.py`、`config.yaml` | 既定Silero/morphと30秒上限、容量、推論期限 |
| `scripts/formal/silero_contract.py`、`scripts/formal/lesson_liveness.py`、`scripts/formal/lesson_endurance.py` | ONNX代役、制御Executor、Socket、仮想時計 |

```powershell
git diff ea925f9ed3be791d96e24faa975a00ec4c81d99c -- server config.yaml scripts/formal/silero_contract.py scripts/formal/lesson_liveness.py scripts/formal/lesson_endurance.py
```

分割位置、持越し、時刻、await境界、容量、Turnの確定条件が変わったら、独立したPCM台帳と期待字幕を見直す。
通常版、各破壊の意図した失敗、復元後の一致まで確認してから、同期コミット、検証器の `SYNC_COMMIT`、基準結果を更新する。
同期先は、対象コードと同じ内容を持ち、基のブランチから取得できるコミットを使う。
未確認の変更が溜まった場合は `/maintain-formal-verification-skill` を使う。

## 保証の限界を守る

Python実コードへの有限入力の全列挙であり、無制限の形式証明や全asyncio順序の網羅ではない。
50分は仮想時間で、ONNX・ASR・MTの計算は代役を使う。
マイク、実モデルの認識・翻訳精度、実時間の性能、LAN、実WebSocket、生徒画面は保証しない。
推論が設定した時間で完了し、正常な生徒接続へ送信できることを前提とする。
途中字幕は既定どおり無効にし、flush時は512サンプル未満の端数がない入力を使う。

30秒は音声分割の上限であり、字幕の到着期限ではない。
32ms単位では上限到達が30.016秒となり、静かな位置への切り戻しにより最初の認識断片は30秒未満になる。
morphが継続語尾を保留するため、最初の確定字幕は次の強制分割を待つ場合がある。

Sileroの本物ONNX照合は [silero-lesson](../silero-lesson/SKILL.md)、永久停止した推論の復旧は [lesson-endurance](../lesson-endurance/SKILL.md)、送信障害は [slow-student-continuity](../slow-student-continuity/SKILL.md) を併用する。
