---
name: lesson-audio-boundary
description: 一時停止・授業終了の直前に残った短い音声が再開後の認識と英中字幕に混ざらず、次の発話の翻訳が続くかを有界検証する。Lesson の停止・再開、VoiceSegmenter の開始判定・プリロール・flush、Pipeline の授業境界を変更するときに使う。
---

# 停止前と再開後の発話を分けて翻訳する

先生が一時停止して再開したときも、授業を終了して次の授業を始めたときも、各区間の音声だけで発話の開始を判定する。既に開いた発話は停止時に確定し、その後の発話も英語と中国語の字幕に順番どおり一度ずつ届く。境界をまたいで開始を判定すると、前の授業の声が新しい授業の認識に入る。

[teacher-continuity](../teacher-continuity/SKILL.md) は受理済み音声を持つ先生の交代を、[silero-lesson](../silero-lesson/SKILL.md) は連続入力中の32 ms判定を扱う。ここでは発話の開始に必要な96 msが揃う前後で、先生が授業を止める。

## 有界検証を実行する

リポジトリ直下で Python 3.12.10 と[固定依存](../../../scripts/formal/requirements.txt)を使う。モデルのダウンロードは不要。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/lesson_audio_boundary.py --mutation-check --json .audit/lesson-audio-boundary.json
```

同じ依存が入った開発機では `.venv/Scripts/python.exe -X utf8 -B scripts/formal/lesson_audio_boundary.py --mutation-check` でも実行できる。結果の読み方は次のとおり。

- `PASS`・終了0なら指定範囲に反例なし。`FAIL`・終了1なら `failed_cases` の引数を `--case` に渡し、入力した音、ASRに実際に渡った音、先生の確定原文、英中の受信字幕を比較する。
- `passed: 256`、`failed: 0`、`continuation_ok: 256` を確認する。[Issue #211](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/211) の修正前は、一時停止と終了で各40通りに停止前の音が混ざった。
- `--mutation-check` は製品の一時コピーを使う。`end 1 2 1` で開始待ちの音声を残す故障を検出する。`end 1 3 3` では確定発話の消失、停止時の音声時刻の巻き戻り、後続の中国語字幕欠落、採番の飛び、終了が一時停止に化ける故障を検出する。`mutations.results` に6件の反例が出る。`restored_control` と `restored_matches` がともに `true` なら、復元後の両対照と全探索が最初と一致した。対照に違反がある場合は、破壊検査の `INCONCLUSIVE` と全探索の反例を確認する。
- `ERROR`・終了2や起動時の例外では検査が完了していない。依存、asyncio のローカルソケット、ワーカーの停止・タイムアウト、破壊箇所の一致を調べる。

## Issue #211 の再現入力を確認する

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/lesson_audio_boundary.py --case end 1 2 1
```

1. 先生が100 msのPCMを1個送る。末尾の完全な判定枠2枚が有音で、64 msだけでは発話が開かない。
2. 先生が授業を終了し、次の授業を開始する。最初の判定枠1枚（32 ms）に別の音を入れ、その後は無音にする。この時点までの期待字幕は0件。
3. `observed.after` の ASR 入力、先生の確定原文、英中字幕がすべて空であることを確認する。修正前は旧1,024サンプルと新512サンプルが一つの発話としてASRに渡っていた。
4. 次に独立した96 msの発話を送り、両言語の新着字幕を確認する。この発話の結果は `observed.continuation` に出る。

## 探索範囲

Python で有限のPCM列を列挙して実際の `Connection.receive` に入力する。授業操作、音声区切り、非同期のASR・MTワーカー、採番、送信キューを同じ製品コードで通し、実際の受信フレームを観測できる。

- 操作は `pause → start` と `end → start`。停止前の100 msパケット数は1〜8個。16 kHzの1,600サンプルとSileroの512サンプルのずれから生じる、全8種類の余り（0〜448、64刻み）を含む。停止前の端数は無音。
- 停止直前の完全な有音枠と再開直後の有音枠はそれぞれ0〜3枚。全組合せは 2 × 8 × 4 × 4 = 256通り。再開後と後続の入力はそれぞれ100 msを16パケットとし、先頭の有音の後は区間末尾まで無音。各観測点でASR・MTと送信を排出する。
- 期待する発話は入力台帳から独立に決める。連続3枚ならその区間の識別音が1件、0〜2枚なら0件。ASRに渡った非ゼロサンプルの値・個数・順序、先生の確定原文、言語別の訳文と発話番号を比較する。新しい正当な発話に旧PCMが付く場合も違反に含む。
- 停止前・再開直後・その次の発話について、処理済みサンプル数とプリロールから開始・終了時刻を求め、字幕履歴の `t_start`・`t_end` と照合する。停止時の端数は捨て、完全な判定枠のぶんだけ再開後の時刻を進める。
- 実Sileroアダプターに対しONNXの1枠ごとの確率だけを決定的に与える。ASRは実際に届いた波形の識別語を読み、MTは原文と言語をそのまま照合できる訳文を返す。任意の波形・操作列、推論失敗・遅延、無音サンプル数、再帰状態の独立性、実時間50分の性能、マイクや画面表示はこの全探索の保証外。

## 出荷モデルで照合する

通常のアプリ依存、出荷した Silero・Haas ONNX・Hy-MT2、日本語音源 `tests/fixtures/ja/{01,02,03}.wav` を使う。音源がない開発機では `./scripts/make_fixture_audio.ps1 -BaseOnly` で生成する。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/lesson_audio_boundary_real.py --action end --json .audit/lesson-audio-boundary-real-end.json
.venv/Scripts/python.exe -X utf8 -B scripts/formal/lesson_audio_boundary_real.py --action pause --json .audit/lesson-audio-boundary-real-pause.json
```

モデルは `config.yaml` の `models.dir` から読み込む。別の場所を使う場合は `--models-dir` を指定する。各操作で、停止直前に短い音声を入れた場合と、何も入れない対照に同じ2発話を送る。修正前は旧授業の有音512サンプルが次のHaas入力の先頭に残っていた。

`old_pcm_absent` は旧音声に固有の2枠をHaas入力から検索する。`first_pcm_matches` と `following_pcm_matches` は入力全体の長さとSHA-256を対照と照合する。両発話の `*_captions_match` で発話番号、日本語原文、英訳、中国語訳が対照と一致することを確認する。検査に使うのは上記の固定音源である。

`first_timestamps_match` と `following_timestamps_match` は、確定した発話の開始・終了時刻を16 kHzのサンプル数で比較する。100 msの前置きで処理されるのは512サンプルの完全な3枠なので、各時刻は前置きなしの対照より1,536サンプル（96 ms）先になる。

## 実装との同期

差分照合の起点は `009ce938301e8fc37fb908bce249d3cda1d50c70`。音声状態の初期化を共通化した `server/audio/vad.py` の検証済み Git blob は `fed452fd2034d739a8cb57f4d14ec66aa28e01e6`。

| 対象 | 照合する処理 |
|---|---|
| `config.yaml`、`server/config.py`、`web/audio-worklet.js` | 既定のSilero・morph、発話の開始・確定条件、先生が送るPCMの粒度 |
| `server/connection.py`、`server/lesson.py` | `receive`、`control`、`_transition`、`_finalize_lesson` の停止・再開・排出順 |
| `server/audio/{ingest,vad}.py` | `pcm16_from_bytes`、`SileroVAD.is_speech` の判定単位、`VoiceSegmenter` の開始待ち・端数・プリロール、`flush` と `reset` の音声状態と時刻 |
| `server/pipeline.py`、`server/asr_backlog.py`、`server/turn/` | `feed_audio`、`flush_audio`、`_asr_worker`、`drain`、Turn確定と音声の順序 |
| `server/caption_{delivery,history}.py`、`server/{roster,delivery}.py`、`server/mt/cache.py` | 確定発話の採番、言語ごとのジョブ、字幕送信、キャッシュ |
| `server/{main,inference,inference_process}.py`、`server/asr/haas_onnx_engine.py`、`server/mt/hymt_engine.py` | `create_app` が選ぶ出荷モデルと子プロセスへの入力 |
| `scripts/formal/{lesson_liveness,silero_contract,lesson_liveness_real}.py` | Socket、識別用MT、ONNX代役の注入箇所と実モデルの観測 |

```powershell
git diff 009ce938301e8fc37fb908bce249d3cda1d50c70 -- config.yaml web/audio-worklet.js server scripts/formal scripts/make_fixture_audio.ps1
git hash-object server/audio/vad.py
```

対象が変わったら、パケット境界と開始条件を実装に照合する。反例が新しく出たら手順と入力を残し、製品の修正は別途扱う。意図した契約変更なら入力台帳・期待値・破壊箇所をそろえる。実装との対応、全探索、破壊検出と復元、実モデル照合を確認してから同期コミットを進める。変更が溜まった場合は `/maintain-formal-verification-skill` を使う。
