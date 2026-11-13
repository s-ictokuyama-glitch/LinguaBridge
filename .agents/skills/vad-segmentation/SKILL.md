---
name: vad-segmentation
description: 音声の強制分割・受信チャンクの端数・flush をまたいで PCM が保たれることを実コードの有界探索で確認する。Use when changing VoiceSegmenter.feed, flush, _process_idle_frame, _maybe_force_close, or the frame, pre-roll, and duration limits they use.
---

# 強制分割後も元の音声を順番どおりに渡す

発話の途中で強制分割しても、確定した Segment をつなぐと元の PCM が順番どおりに一度ずつ現れる。
違反すると、後段の音声認識に欠けた音声や重複した音声が入り、字幕と授業記録に影響する。

## 実行する

リポジトリ直下で Python 3.12.10 と NumPy 2.5.4 を使う。

```powershell
uv run --python 3.12.10 --no-project --with numpy==2.5.4 python -X utf8 -B scripts/formal/vad_segmentation.py --mutation-check
```

同じ版の既存環境では `.venv/Scripts/python.exe -X utf8 -B scripts/formal/vad_segmentation.py --mutation-check` も使える。
終了コード0、`status` が `PASS`、`scenarios` が129,768、`mutations` の6件すべてが `detected: true` であることを確認する。
`--mutation-check` は一時ファイルに書いた `vad.py` の破壊版を実行する。

## 反例を判断する

終了コード1の `violation` に最初の反例が出る。`frames` の `s` は無音、`q` は弱い音声、`v` は強い音声を表す。`cuts` は入力先頭からのチャンク境界のサンプル数、`tail` は末尾の不完全なフレームの有無である。
`pcm_partition` なら `expected` と `actual` の PCM と開始時刻を照合する。`segment_size` なら確定 Segment のサンプル数を確認する。
例えば `vvsvv` で無音の直後に切ったとき、4フレーム目は次の Segment の先頭に来る。`drop_carry` はこれを捨てるため、開始時刻が0.75秒から1秒へずれる。
実装で成立する入力に対する違反は、手順と影響を報告する。前提の読み違いなら検証器を修正する。例外や破壊箇所の不一致は検証環境や検証器を調べる。

## 探索範囲

Python で実際の `VoiceSegmenter` と `EnergyVAD` を実行し、確定 PCM を元の入力と直接比較する。
8 Hz、1フレーム2サンプル、強制分割1秒、確定無音750 ms、プリロール500 msで探索する。
音声で始まり音声で終わる1〜8フレームの全列から、3フレーム以上の連続無音を含む列を除く。
チャンクは一括、すべての1箇所分割、1サンプルずつの各形を使い、末尾の端数の有無を掛け合わせる。最後に `flush` を2回呼ぶ。
確定 Segment の PCM と `t_start` が入力を順番どおりに分割し、各 Segment が1〜4フレームで収まり、2回目の `flush` が空になることを確認する。
任意の多分割、自然な無音確定、`reset`、`t_end`、切断位置の選び方、SileroVAD、既定設定の長い音声、ASR 以降は探索範囲外である。

## 実装との同期を確認する

最終同期コミットは `4efcb9f6edd2b1e39aea9c173d9b8b29870861f1`。

| ファイル | 照合する処理 |
|---|---|
| `server/audio/vad.py` | `VoiceSegmenter.__init__`、`feed`、`flush`、`_process_frame`、`_process_idle_frame`、`_maybe_force_close`、`_close`、`_reset_utterance`。判定器には `EnergyVAD.is_speech` を使用する |
| `server/pipeline.py` | `feed_audio` と `flush_audio` が確定 Segment を ASR へ渡す契約。呼び出し側の照合のみ |

```powershell
git diff 4efcb9f6edd2b1e39aea9c173d9b8b29870861f1 -- server/audio/vad.py server/pipeline.py
```

フレームの端数、無音確定の境界、持ち越す PCM と開始時刻、プリロール、`flush` の契約を照合する。破壊の置換箇所が変わった場合は、同じ性質を壊す変更へ更新する。
実装との対応を確認し、全探索と6種類の破壊検出を再実行した後に、この同期コミットと検証器の `SYNC_COMMIT` を更新する。未確認の変更を取り込んだ場合は `/maintain-formal-verification-skill` を使う。
