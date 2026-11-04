---
name: turn-assembly
description: 認識断片の保持・Turn確定条件・途中字幕の発話IDと版番号を実コードの有界探索で確認する。Use when changing TurnAssembler, _Pending, TurnPart, Turn, or their partial identity, flush, reset, count and span limits, and SurfaceBoundaryClassifier integration.
---

# 認識断片を同じ発話へ一度ずつ確定する

認識済みの断片を入力順に一度ずつ確定し、途中字幕を同じ発話 ID へ結び付ける。
違反すると、全生徒の字幕・翻訳・授業記録が欠落・重複するか、先生の途中字幕が別発話へ混ざる。

## 実行する

リポジトリ直下で実行する。Python 3.12.10 を使い、追加依存は不要である。

```powershell
uv run --python 3.12.10 --no-project python -X utf8 -B scripts/formal/turn_assembly.py --mutation-check --json .audit/turn-assembly-result.json
```

同じ版の既存環境では `.venv/Scripts/python.exe -X utf8 -B scripts/formal/turn_assembly.py --mutation-check` も使える。
Windows の制限環境で一時ディレクトリの書込が拒否されたら、実行権限を確認する。
終了コード0と `status: PASS`、123,764ケースの `failed: 0`、12破壊全ての `healthy: PASS` と `detected: true` を確認する。
終了コード1と `FAIL` は反例を示す。Traceback、タイムアウト、破壊箇所の不一致は検証器か環境の失敗として調査する。
現行コードの期待値は PASS であり、[基準結果](../../../scripts/formal/turn_assembly_baseline.json) と比較できる。
元コードに反例がある場合は、その反例を出力して破壊検出を省略する。`mutation_check_skipped` を確認し、先に元コードの反例を判断する。

## 反例を判断する

`violations` の `trace` を [操作と検証範囲](../../../scripts/formal/turn_assembly.md) で読み、実コードの呼出し順と `expected`、`actual` を照合する。
`conservation` は確定文と件数、`metadata` は時刻と合計、`termination` は確定条件、`turn_identity` は予約・解放と連番、`preview_text` と `preview_revision` は途中字幕、`pending_state` は保留の有無である。
末尾の2回の `flush` は探索長の外に追加した観測であり、2回目は何も返さないことを要求する。
reset だけは未確定断片を捨てる操作である。`discard_reserved` は断片があれば ID を保持する。
入力と同期呼出しが製品で成立するなら不具合として報告する。架空の割込みや誤った分類・設定の期待値なら検証器を修正する。
性質を弱めて反例を消さず、製品コードの修正は依頼された場合に行う。

## 実装との同期を確認する

最終同期コミットは `d78968533639ca29108318dd9e572407f26c1c10`。

| ファイル | 照合する処理 |
|---|---|
| `server/turn/assembler.py` | `TurnPart`、`Turn`、`_Pending`、`TurnAssembler` の全メソッドとプロパティ |
| `server/turn/boundary.py` | `SurfaceBoundaryClassifier.classify`、`BoundaryClass.ends_turn`。実行するのは入力表の代表語尾のみ |
| `server/pipeline.py` | `_asr_worker`、`_process_interim`、`_process_segment`、`_close_pending_turn`、`flush_audio` の呼出し契約のみ。実行対象外 |
| `server/lesson.py` | `_transition` が live を離れるときに `flush_audio` を呼ぶ契約のみ。実行対象外 |
| `server/config.py` | `TurnMorphConfig` の個数・時間上限との対応のみ。実行対象外 |

```powershell
git diff d78968533639ca29108318dd9e572407f26c1c10 -- server/turn/assembler.py server/turn/boundary.py server/pipeline.py server/lesson.py server/config.py
```

保留の所有権、確定と破棄の条件、ID の寿命、版番号、時間幅と音声長の違い、単一ワーカー契約を照合する。
`--mutation-check` は製品コードの一時コピーを壊す。置換箇所が変わったら、新実装で同じ性質を壊す操作へ更新する。
製品との一致、全探索、12破壊の検出、破壊後の元コードの PASS を確認した後だけ、この同期コミットと検証器の `SYNC_COMMIT` を同じ値へ進める。
未確認の変更を取り込んだ場合は `/maintain-formal-verification-skill` を使う。

## 保証の限界を守る

Python 実装への有限操作列の全列挙であり、無制限の形式証明ではない。独立した TLA+ モデルは使わない。
13操作の長さ0〜4の全列と末尾 flush 2回を、simple と morph の合計4設定で実行する。
断片は最大4件、音声長は0.5秒と1.5秒、空白時間は0.5秒刻みとする。
文末・継続語尾は入力表の代表例に限り、上限の到達は個数2・時間1秒の設定で確認する。
既定の12断片・30秒、全語彙、VAD、ASR、Pipeline の stale 判定、実際の並行処理、翻訳・配信・記録・画面は対象外である。
