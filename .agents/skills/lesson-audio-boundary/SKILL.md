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
- 同期時点は256通り中176通り成功、80通り違反。一時停止と終了で各40通りに停止前の音が混ざった。その後の独立した発話は全256通りで両言語に届いた（`continuation_ok`）。現行の違反を合格条件に組み込まない。
- `--mutation-check` は正常対照 `--case end 1 3 3` を一時コピーで壊す。`mutations.results` に、停止時の発話消失、再開時の旧PCM混入、後続の中国語字幕欠落、採番の飛び、終了が一時停止に化ける場合の5件の反例が出る。`restored_control` と `restored_matches` がともに `true` なら、復元後の対照と全探索が最初と一致している。対照で既に製品の違反が出た場合、破壊検査は `INCONCLUSIVE`、全探索の反例は `FAIL` のまま残す。
- `ERROR`・終了2や起動時の例外では検査が完了していない。依存、asyncio のローカルソケット、ワーカーの停止・タイムアウト、破壊箇所の一致を調べる。

## 反例を再現する

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/lesson_audio_boundary.py --case end 1 2 1
```

1. 先生が100 msのPCMを1個送る。末尾の完全な判定枠2枚が有音で、64 msだけでは発話が開かない。
2. 先生が授業を終了し、次の授業を開始する。最初の判定枠1枚（32 ms）に別の音を入れ、その後は無音にする。この時点までの期待字幕は0件。
3. 現行の `VoiceSegmenter.flush` は発話がまだ開いていないとき、開始待ちの連続枚数とプリロールを保持する。再開後、旧1,024サンプルと新512サンプルをまとめてASRへ渡す。識別用のASR・MTでは、先生と英中生徒に両方の識別語を含む字幕が1件届く。
4. 次に独立した96 msの発話を送り、両言語の新着字幕を確認する。この発話の結果は `observed.continuation` に出る。

## 探索範囲

Python で有限のPCM列を列挙して実際の `Connection.receive` に入力する。授業操作、音声区切り、非同期のASR・MTワーカー、採番、送信キューを同じ製品コードで通し、実際の受信フレームを観測できる。

- 操作は `pause → start` と `end → start`。停止前の100 msパケット数は1〜8個。16 kHzの1,600サンプルとSileroの512サンプルのずれから生じる、全8種類の余り（0〜448、64刻み）を含む。停止前の端数は無音。
- 停止直前の完全な有音枠と再開直後の有音枠はそれぞれ0〜3枚。全組合せは 2 × 8 × 4 × 4 = 256通り。再開後と後続の入力はそれぞれ100 msを16パケットとし、先頭の有音の後は区間末尾まで無音。各観測点でASR・MTと送信を排出する。
- 期待する発話は入力台帳から独立に決める。連続3枚ならその区間の識別音が1件、0〜2枚なら0件。ASRに渡った非ゼロサンプルの値・個数・順序、先生の確定原文、言語別の訳文と発話番号を比較する。新しい正当な発話に旧PCMが付く場合も違反に含む。
- 実Sileroアダプターに対しONNXの1枠ごとの確率だけを決定的に与える。ASRは実際に届いた波形の識別語を読み、MTは原文と言語をそのまま照合できる訳文を返す。任意の波形・操作列、推論失敗・遅延、無音サンプル数、再帰状態の独立性、実時間50分の性能、マイクや画面表示はこの全探索の保証外。

## 出荷モデルで照合する

通常のアプリ依存、出荷した Silero・Haas ONNX・Hy-MT2、日本語音源 `tests/fixtures/ja/{01,02,03}.wav` を使う。音源がない開発機では `./scripts/make_fixture_audio.ps1 -BaseOnly` で生成する。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/lesson_audio_boundary_real.py --json .audit/lesson-audio-boundary-real.json
```

モデルの場所は `config.yaml` の `models.dir`、別の場所では `--models-dir` を指定する。実際の授業終了・開始を通し、新授業の同一音源を、旧授業の短い前置きあり・なしの2回で比較する。同期時点では前置きに固有の512サンプル（32 ms）が次のHaas入力の先頭に現れ、`old_pcm_absent` が失敗した。この項目が探すのは、旧授業に固有の有音2枠それぞれの完全一致。1枠未満の混入は見逃しうる。再開直後とその次の日本語原文・英訳・中国語訳は対照と一致した。音声やモデルを更新した場合は、対照のASR入力・両言語の受信内容と、旧音声の位置を確認する。固定音源での一致は授業全般の認識・翻訳精度を保証しない。

## 実装との同期

同期コミットは `6689b3f6504cdf1f6ed168ad518bbcea173f3d51`。

| 対象 | 照合する処理 |
|---|---|
| `config.yaml`、`server/config.py`、`web/audio-worklet.js` | 既定のSilero・morph、発話の開始・確定条件、先生が送るPCMの粒度 |
| `server/connection.py`、`server/lesson.py` | `receive`、`control`、`_transition`、`_finalize_lesson` の停止・再開・排出順 |
| `server/audio/{ingest,vad}.py` | `pcm16_from_bytes`、`VoiceSegmenter.feed/flush/_process_idle_frame/_close`、`SileroVAD.is_speech` の判定単位、開始待ち、端数とプリロール |
| `server/pipeline.py`、`server/asr_backlog.py`、`server/turn/` | `feed_audio`、`flush_audio`、`_asr_worker`、`drain`、Turn確定と音声の順序 |
| `server/caption_{delivery,history}.py`、`server/{roster,delivery}.py`、`server/mt/cache.py` | 確定発話の採番、言語ごとのジョブ、字幕送信、キャッシュ |
| `server/{main,inference,inference_process}.py`、`server/asr/haas_onnx_engine.py`、`server/mt/hymt_engine.py` | `create_app` が選ぶ出荷モデルと子プロセスへの入力 |
| `scripts/formal/{lesson_liveness,silero_contract,lesson_liveness_real}.py` | Socket、識別用MT、ONNX代役の注入箇所と実モデルの観測 |

```powershell
git diff 6689b3f6504cdf1f6ed168ad518bbcea173f3d51 -- config.yaml web/audio-worklet.js server scripts/formal scripts/make_fixture_audio.ps1
```

対象が変わったら、パケット境界と開始条件を実装に照合する。反例が新しく出たら手順と入力を残し、製品の修正は別途扱う。意図した契約変更なら入力台帳・期待値・破壊箇所をそろえる。実装との対応、全探索、破壊検出と復元、実モデル照合を確認してから同期コミットを進める。変更が溜まった場合は `/maintain-formal-verification-skill` を使う。
