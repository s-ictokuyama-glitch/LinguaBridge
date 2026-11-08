# 授業動画で ASR 候補を比較する

既存の評価環境でリポジトリのルートから実行する。動画の書き起こし、区間音声、下書き、正解文は教材の複製なので、リポジトリ外に保存する。

## 候補を同じ Segment で書き起こす

```powershell
$evalDir = Join-Path $env:LOCALAPPDATA 'LinguaBridge/evaluation'
New-Item -ItemType Directory -Force $evalDir | Out-Null
$video = 'TestData/testdata.mp4'

python scripts/lesson_asr_eval.py $video --candidate-out "$evalDir/reazon.json"
python scripts/lesson_asr_eval.py $video --config-override config.accuracy.yaml --segment-config config.yaml --candidate-out "$evalDir/kotoba.json"
```

`--config` と `--config-override` はサーバーと同じように設定を読み込む。通常はその設定の VAD と Turn の値で区切る。上の Kotoba Whisper の例は、精度プロファイルから ASR の設定を読み込み、`--segment-config` で区切りを ReazonSpeech と揃える。

`--candidate-out` には入力動画の SHA256 と、Segment 番号ごとの書き起こし、音声の開始・終了サンプル番号が入る。区間には推論に渡した前後の無音も含む。書き起こしだけが必要なら `--out` を使う。コマンドが表示する `asr` は、warmup を除いた各 Segment の推論時間の合計である。

## 下書きを作り、正解文を取り出す

```powershell
python -m scripts.lesson_asr_reference build $video --candidate Reazon "$evalDir/reazon.json" --candidate Kotoba "$evalDir/kotoba.json" --out-dir "$evalDir/draft"
```

`draft/draft.md` を Markdown プレビューで開くと、Segment の時刻、区間音声へのリンク、各候補の書き起こしが並ぶ。太字は別の候補と異なる文字を示す。確定文の初期値には、他候補との文字編集距離の合計が最も小さい候補を使う。同点なら文字列順で決めるため、コマンドに候補を並べる順序を変えても初期値は変わらない。

区間音声を聞き、Markdown の「確定文」直下のコードブロックの中だけを編集する。候補を追加するときは `--candidate` を増やし、新しい `--out-dir` を指定する。既存のディレクトリへの生成はエラーになる。

```powershell
python -m scripts.lesson_asr_reference extract "$evalDir/draft/draft.md" --out "$evalDir/ref.json" --edits "$evalDir/edited.json"
python scripts/lesson_asr_eval.py $video --ref "$evalDir/ref.json" --terms server/asr/hotwords/geography.txt
python scripts/lesson_asr_eval.py $video --config-override config.accuracy.yaml --segment-config config.yaml --ref "$evalDir/ref.json" --terms server/asr/hotwords/geography.txt
```

`edited.json` には初期値から変えた Segment 番号が入る。既存の正解文と変更一覧は上書きしない。再度取り出すときは新しい出力名を指定する。

## 計測結果を採用基準と照合する

計測値を次の形で JSON にまとめる。CER は割合、遅延は ms、メモリと配布容量の増加はバイトで記録する。

```json
{
  "baseline_cer": 0.0705,
  "candidates": {
    "candidate-a": {
      "cer": 0.0564,
      "median_delay_ms": 5000,
      "max_delay_ms": 8000,
      "peak_rss_bytes": 4500000000,
      "package_growth_bytes": 1000000000
    }
  }
}
```

```powershell
python -m scripts.asr_adoption "$evalDir/measurements.json"
```

出力には候補ごとの合否と不合格理由、採用した候補名が入る。採用できる候補が無ければ `adopted` は `null` になる。
