---
name: silero-lesson
description: 既定Sileroの状態・文脈・32ms処理を実コードの有界探索で検査し、morphの音声入力から英中字幕まで50分仮想入力が続くことを確認する。Use when changing SileroVAD, its ONNX API, Pipeline VAD frame sizing, or sustained Silero/morph caption delivery.
---

# 既定Silero経路の授業継続を検査する

先生のPCMを32msごとに正しくSileroへ渡し、指定した授業入力で英中字幕を順番どおり一度ずつ届け続ける。
違反すると、発話の欠落、誤った区切り、字幕の停止や処理待ちの増加が起きる。

energy判定を使う [morph-lesson](../morph-lesson/SKILL.md) と、Silero対象外の [vad-segmentation](../vad-segmentation/SKILL.md) を補う。

## 全検証を実行する

リポジトリ直下でPython 3.12.10と固定依存を使う。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/silero_lesson.py --mutation-check --json .audit/silero-lesson.json
```

同じ依存がある `.venv/Scripts/python.exe` でも実行できる。Windowsの制限環境でasyncio初期化が停止する場合は実行権限を確認する。環境による停止を製品の反例と数えない。

終了コード0と `status: PASS`、以下の結果を確認する。[基準結果](../../../scripts/formal/silero_lesson_baseline.json) と [範囲・選定理由](../../../scripts/formal/silero_lesson.md) を読む。

- `contract` は341列、padding固定6ケース、1,298呼出し。
- `bounded` は777候補、`legal_passed: 324`、`failed: 0`。
- `timing` は開始3枚、無音によるSegment確定16枚、Turn確定32枚。
- `sustained` は3000秒の2構成。各30,000受信チャンク、93,750 VADフレーム、英中各1,125字幕。
- 20件の `mutations` すべてで `healthy: PASS` と `detected: true`。`restored: PASS` と `restored_matches: true`。

`--probe contract|pipeline|timing|sustained` は名前を1つ指定して切り分ける。`--probe sustained --seconds 160` は短い入力だけを実行する。短縮実行を最終確認の代用にしない。

## 同梱ONNXとの一致を確認する

Silero本体、依存、配布パッケージを変更した場合は本物モデルも確認する。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements-silero.txt python -X utf8 -B scripts/formal/silero_lesson.py --native-only --mutation-check --json .audit/silero-native.json
```

既存アプリ依存が同じ版なら `.venv/Scripts/python.exe` でもよい。[本物ONNX基準結果](../../../scripts/formal/silero_native_baseline.json) の入出力仕様、依存版、モデルSHA256、14固定入力、reset後の再現を確認する。正規化を壊した1件の検出と復元も確認する。ONNXが返すAPI仕様の変更や依存不足は `ERROR` として扱い、代役の成功だけで依存更新を承認しない。

## 反例を判断する

終了コード1と `FAIL` は性質の違反、2と `ERROR` は環境・検証器・依存APIの失敗。終了コード1を期待するテストで包み、現行コードの失敗を成功扱いにしない。

`violations` の `property`、`trace`、`expected`、`actual` を読む。正規化・文脈・h/cの期待値は独立PCM台帳とONNX代役が返した状態から作る。字幕の期待値は入力計画から作り、受信済みの全文・seq・言語・順序と比較する。ASR・MTの結果を期待値として採用しない。

32msでは500msの無音を16枚、1000msを32枚へ切り上げる。energyの100ms設定をそのまま期待値へ使わない。flushは端数を捨てるがSilero状態をresetしない。操作列のflushは端数0だけを扱う。実コードのawait境界、確定条件、入力台帳と照合し、製品で成立する違反なら反例を保持する。性質を弱めず、製品修正は依頼された場合に行う。

故障注入は製品ソースの一時コピーで行う。指定した性質の違反と終了コード1だけを検出成功とする。Traceback、時間切れ、結果なし、置換箇所不一致は成功に数えない。最後に元コードが同じ結果へ戻ることを確認する。

## 実装との同期を確認する

同期コミットは `c7d3851dfb2327db6e9574325b7d957a4c4f2216`。

| 対象 | 照合する契約 |
|---|---|
| `server/audio/vad.py` | `SileroVAD.__init__`、`is_speech`、`reset`、`build_frame_vad`、`VoiceSegmenter.feed` と32msでの開始・無音確定 |
| `server/audio/ingest.py`、`server/pipeline.py` | PCM受理、`frame_ms`接続、ASR worker、morph確定、flush、drain、stop |
| `server/asr_backlog.py`、`server/turn/assembler.py`、`server/turn/boundary.py` | 音声と境界の順序、容量、文中断片と文末の組立 |
| `server/caption_delivery.py`、`server/caption_history.py`、`server/mt/cache.py` | 翻訳待ち、確定字幕、履歴・キャッシュの保持 |
| `server/roster.py`、`server/delivery.py`、`server/ws_protocol.py` | 実送信処理から制御Socketまでの字幕形式と順序 |
| `server/inference.py`、`server/asr/base.py`、`server/asr/hallucination.py`、`server/mt/base.py` | 制御Futureの完了、計算結果の受渡し |
| `server/config.py`、`config.yaml`、`requirements.txt` | 出荷Silero/morph設定と依存 |
| `scripts/formal/morph_lesson.py`、`scripts/formal/lesson_liveness.py`、`scripts/formal/lesson_endurance.py` | 再利用する期待発話、比較、排出、Executor、Socket、仮想時計 |

```powershell
git diff c7d3851dfb2327db6e9574325b7d957a4c4f2216 -- server config.yaml requirements.txt scripts/formal/morph_lesson.py scripts/formal/lesson_liveness.py scripts/formal/lesson_endurance.py
```

状態・入力仕様・容量・await・確定条件が変わったら、期待値と故障注入を見直す。実装との対応、全探索、50分2構成、破壊検出と復元、本物ONNX照合を確認した後だけ同期コミット、検証器の `SYNC_COMMIT`、基準結果を更新する。同期先には対象コードと同じ内容を持つ基のブランチのコミットを使い、新規cloneで解決できることを確認する。未確認の変更が溜まったら `/maintain-formal-verification-skill` を使う。

## 保証範囲を守る

Python実コードへの有限入力全列挙であり、無制限の形式証明ではない。契約は4操作の長さ0〜4、授業は6操作の長さ0〜3を3送信幅で調べる。各送信・操作後に16回イベントループを進める。全asyncio順序の網羅ではない。

50分は仮想時間。ONNX・ASR・MTの計算は代役で、出荷Sileroラッパーと32ms Segmenter、morph、配信は実装を使う。ASRと各言語のMTは0.2秒または0.8仮想秒。入力計画上の確定時点から8秒を字幕期限にし、末尾の排出は10秒。MT容量は1件、統計間隔は3600秒。保持量は観測コンテナーとタスク数だけを扱う。

本物ONNXの検査は14固定合成入力の入出力一致だけを確認する。教室雑音でのSilero精度、ASR・MTの意味精度、実モデル速度、実時間50分性能、物理マイク、LAN、ブラウザー、推論ハング、再接続、記録、partial、端数を伴うflush、`VoiceSegmenter.reset` との連動、30秒以上の強制分割は対象外。実時間性能は [lesson-endurance](../lesson-endurance/SKILL.md) を使う。
