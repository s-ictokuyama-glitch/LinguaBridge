---
name: hymt-translation
description: 授業中に Hy-MT2 が一方の言語で例外・空訳を返したとき、後続発話と同じ原文の再翻訳が英中字幕へ届くかを実コードで有界検証する。HyMt2Engine.translate、CaptionDelivery の翻訳失敗処理、発話をまたぐ翻訳キャッシュ、字幕の欠落や空訳の再配信を調べるときに使う。
---

# 翻訳が一度失敗した後も授業の字幕を届ける

先生が話したとき、訳が得られた言語には対応する字幕が一度だけ届く。訳が得られなかった言語には空の確定字幕を送らず、翻訳器が回復した後は同じ原文にも新しい文にも正しい言語の字幕を届ける。

空訳を成功として保存すると、先生が同じ説明をやり直しても生徒の訳文が空のままになる。受け入れ条件の根拠は [実モデルの字幕照合](../../../scripts/formal/lesson_liveness_real.py) と [英中の空訳を拒む授業性能検査](../../../tests/unit/test_perf01_evidence.py) にある。

## 実行する

リポジトリのルートから Python 3.12.10 と [固定依存](../../../scripts/formal/requirements.txt) で実行する。モデルファイルの取得は不要。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/hymt_translation.py --mutation-check --json .audit/hymt-translation.json
```

同じ環境を用意した Windows では `.venv/Scripts/python.exe -X utf8 -B scripts/formal/hymt_translation.py --mutation-check --json .audit/hymt-translation.json` を使える。1列だけ再現するには `--mutation-check` の代わりに `--case ok empty AB on` を渡す。順に、初回の英語・中国語の応答、続く2発話、キャッシュの設定を表す。

- 終了コード **0 / PASS** は全ケースで期待どおりの字幕を受信したことを示す。
- **1 / FAIL** では `first_counterexamples` を読む。`pcm` は入力した発話の列、`calls` は実際にモデル境界へ届いた原文・言語・応答。`expected` と `actual` の字幕は順に発話番号、日本語、言語、訳文。修正前に見つかった反例が再発した場合も終了コード1で返す。
- **2 / ERROR** は検査が完了しなかったことを示す。たとえば期限超過では、環境で asyncio が起動できるか、ワーカーと送信キューの排出が進むかを確認する。破壊箇所の置換不一致では対応する製品コードを読み直す。

## 入力と境界を把握する

Python で有限入力を全列挙し、実装へ直接渡す。A は「水は液体です。」、B は「氷は固体です。」、C は「次は実験です。」。各発話は 16 kHz の PCM16、200 ms の有音と1秒の無音で作る。

1. 先生と英中の生徒各1人を接続し、最初に A を入力する。生成結果は言語ごとに、正常・例外・空文字・空白だけの4通りを与える。
2. 初回の翻訳と送信を排出し、両言語のモデル呼出しと、受信した字幕を照合する。以後の生成要求はすべて成功する。
3. AA、AB、BA、BB のいずれかと、まだ使っていない C を続けて入力する。この3発話の間では排出を待たない。最後に先生の全確定原文と生徒の受信順・発話番号・原文・言語・訳文を照合する。
4. 出荷時のキャッシュ（同期時点で512件）とキャッシュ無効で繰り返す。英4 × 中4 × 後続列4 × キャッシュ2 の **128ケース**。

対象は実際の `Pipeline.feed_audio`、morph、Hy-MT2 のプロンプト生成・応答取り出し、推論の例外処理、翻訳キャッシュ、確定字幕の送信キュー。音声判定には energy、認識には波形の識別子を日本語に戻す代役、llama.cpp には実際に受け取った言語と原文に応じて固定の訳または指定の失敗を返す代役を使う。生成中のスレッド順序は全列挙しない。マイク・LAN・ブラウザー表示、実モデルが空訳を出す頻度、翻訳の品質、実時間50分の性能はこの探索の範囲外。

## Issue #207 の回帰を確認する

全128ケースが通り、終了コード0、`passed: 128`、`failed: 0` になることを確認する。修正前は96ケースで空字幕が届き、うち36ケースで回復後も同じ原文に空字幕が届いた。[Issue #207](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/207)

`--case ok empty AB on` は、中国語の初回 A に空文字を返す。初回は英語だけが届き、次の A では中国語を再翻訳して発話2の字幕が届く。`empty` を `whitespace` に変えても同じ結果になる。後続の B と C は英中ともに届く。

破壊検査は通る対照列 `ok error AB on` を使い、一時コピーで次を確認する。

| 製品に加える破壊 | 検出する違反 |
|---|---|
| 中国語のプロンプトを英語にする | 初回の言語別要求と字幕が一致しない |
| 翻訳例外の際に空文字をキャッシュに保存する | 回復後、再度の A に空の字幕が届く |
| 先生へ送る発話番号を1つ増やす | 音声入力と先生の確定原文の番号がずれる |

`mutations` の3件と `restored_matches: true` を確認する。元の実装で全128ケースを再実行し、件数と反例の一致を見ている。

実モデルでも確認するときは [lesson-liveness の実モデル手順](../lesson-liveness/SKILL.md) を使う。同期時には `tests/fixtures/ja/03.wav` を32回入力し、既定の Silero・Haas・morph・Hy-MT2 で確定32件、翻訳64回、空でない英中各32字幕を受信した。この反復ではキャッシュを無効にし、全発話の翻訳を実行した。

## 実装との同期を保つ

差分照合の起点は `0fc3bc541587ba8cd729a929a56a4fa5f52c9df9`。ここから Issue #207 で変更した `server/caption_delivery.py` は Git blob `9dda72986eb4793a3bd0c5b3278ca88badc81992` で検証した。

| 対象 | 変更時に照合する処理 |
|---|---|
| `server/mt/hymt_engine.py` | `warmup`、`build_prompt`、`translate` のモデル呼出しと応答形式 |
| `server/caption_delivery.py`、`server/mt/cache.py`、`server/caption_history.py` | `confirm`、`publish`、`_translate`、`_translation_from_cache`、`_process_mt_job`、キャッシュのキーと空値、採番 |
| `server/pipeline.py`、`server/inference.py` | 起動・音声受理・Turn確定・排出・停止、`InferenceRunner.run` の成功と失敗 |
| `server/audio/`、`server/asr_backlog.py`、`server/asr/base.py`、`server/asr/hallucination.py`、`server/turn/` | PCM変換、セグメント化、認識断片の採否とmorphの確定 |
| `server/roster.py`、`server/delivery.py`、`server/ws_protocol.py` | 言語別の受信者、送信、確定原文と字幕の形式 |
| `config.yaml`、`server/config.py`、`scripts/formal/lesson_liveness.py` | 出荷設定、受信Socket代役 |

```powershell
git diff 0fc3bc541587ba8cd729a929a56a4fa5f52c9df9 -- server config.yaml scripts/formal/lesson_liveness.py
git hash-object server/caption_delivery.py
```

変更後は、最初の反例を1列で再実行し、実際のモデル要求、失敗した言語と届いた字幕を上表の経路に照合する。プロンプトの形式変更で代役が原文や言語を識別できない場合は、モデルが実際に受け取る引数を確認して代役を更新する。製品の反例が残る場合は期待値を保つ。意図した契約変更はその根拠とともに入力・期待値に反映する。全探索と3破壊を再実行して復元結果を確認した後に、同期コミットと反例の記録を更新する。未検証の変更が重なったときは `/maintain-formal-verification-skill` を使う。
