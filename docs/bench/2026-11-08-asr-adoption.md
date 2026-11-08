# 授業動画での ASR 採用評価

開発機の Ryzen 7 8840U、Windows、CPU で、ReazonSpeech と Haas-ja 系の5構成を比較した。採用条件は、現行比で CER が相対20%以上改善し、字幕遅延の中央値が5秒以下、最大が8秒以下、サーバーのピーク RSS が4.5 GB 以下、配布容量の増加が1 GB 以下であること。学校の i5 では未測定。Python 3.12.10、sherpa-onnx 1.13.8、ONNX Runtime 1.31.0、speech.cpp 0.10.1、llama-cpp-python 0.3.33 を使った。

判定関数が選んだ採用候補は **Haas-ja Kizuna int8**。CER は15.29%から2.83%に下がり、相対81.5%改善した。5回の全編リプレイで字幕の欠落はなく、最も厳しい回の最大遅延は7,985 msだった。8秒の上限まで15 ms のため、この結果の適用範囲は開発機と今回の条件に限る。配布 ZIP を組んだ状態での確認は #167 で行う。 [計測値と判定結果](2026-11-08-asr-adoption.json)

## 正解文と採点

7候補の出力から、他の候補との文字編集距離の合計が最小の文を区間ごとに選び、下書きにした。人が50区間の音声を聞いて確定文を確認し、22区間を修正した。聞き逃した箇所には候補に有利な下書きが残りうる。区間音声の間にある空白と動画末尾は聞き直していない。動画、書き起こし、区間音声、正解文は評価ディレクトリで管理する。

同じ VAD と Turn の設定で動画を50 Segment に分け、候補間で動画の SHA256 と各区間のサンプル範囲が一致することを検証する。CER は置換・脱落・挿入の合計を正解文の文字数で割る。既存の `text_metrics` と同じく、比較前に Unicode 正規化を行い、空白と句読点を除く。Kotoba は正解文の下書きに使った比較用モデルで、今回の採用候補には含めない。

調整案は採点前に1構成につき1つ固定した。ReazonSpeech は現在の製品設定を調整後として使う。素の設定は探索を greedy に戻し、教科用語、blank penalty、再デコード補完を無効にする。先頭の600 ms の無音はエンジンの入力処理として両方で揃える。Haas fp32 は greedy からビーム探索と教科用語を使う設定に変える。語彙にない文字を含む用語は辞書から省く。Kizuna は認識の設定項目がないため CPU スレッドを4から6に変える。speech.cpp は4スレッドのまま、同じ教科用語をカスタム語彙として渡す。全構成で CPU を使う。

| 構成 | 素の CER | 置換・脱落・挿入 | 調整後 CER | 置換・脱落・挿入 |
| --- | ---: | ---: | ---: | ---: |
| ReazonSpeech | 23.16% | 149・588・40 | 15.29% | 171・270・72 |
| Haas sherpa fp32 | 3.64% | 65・26・31 | 3.43% | 62・25・28 |
| Kizuna int4 | 3.55% | 73・24・22 | 3.55% | 73・24・22 |
| Kizuna int8 | 2.83% | 65・16・14 | 2.83% | 65・16・14 |
| speech.cpp Q4_K | 3.01% | 56・23・22 | 2.98% | 59・20・21 |
| speech.cpp Q8_0 | 2.27% | 52・16・8 | 2.41% | 59・13・9 |

正解文は正規化後で3,355文字。各設定で1回、同じ音声区間を書き起こした。Kizuna のスレッド数を変えても CER の内訳は変わらなかった。Q8_0 では調整後に脱落が減り、置換と挿入が増えた。採用判定には事前に決めた調整後の設定を使う。

## 実時間リプレイと配布容量

調整後の設定で、元動画の音声を16 kHz のモノラル PCM に変換し、先生の WebSocket に等速で送る。生徒2接続で英語と簡体字中国語の字幕を受ける。翻訳は製品と同じ Hy-MT2 の4スレッド構成とし、翻訳キャッシュを無効にする。各回で送信した音声のバイト数と ASR 呼び出し数を照合する。教師側で確定した Turn が2言語に1回ずつ届いたことも確認する。

遅延は製品の字幕に含まれる `delay_ms` で測る。Turn が確定した時点から字幕を送出するまでの値で、発話中と確定前の待ち時間は含まない。RSS はモデルの準備完了後から終了処理まで、サーバーと子孫プロセスを2秒間隔で合計した最大値。speech.cpp の worker も合計に含む。RSS の観測間隔が6秒を超えるか、再生時間が動画の長さから1秒を超えてずれる場合は、その試行を採用判定から外す。再生は開発機上で順に行い、他の構成の推論を同時に走らせない。

配布容量は、ReazonSpeech を残した現行構成に候補の実ファイルを追加する場合の見積もり。現行ビルダーはモデルを無圧縮で ZIP に格納する。Haas fp32 は推論ファイル5点とライセンス類、Kizuna は選択した量子化と共通ファイル、固定 manifest、ライセンス類を数えた。sherpa-onnx と ONNX Runtime は同じバージョンを両構成で使う前提。speech.cpp は GGUF と未圧縮の `speech.exe` を数えた。Vulkan ローダー、上流のライセンス類、ZIP のメタデータはこの集計に含まない。採用する構成があれば、組み込んだ配布 ZIP で差分を確認する。

各構成で全編を1回再生した。初回で遅延、RSS、配信の条件を満たした候補は、調整後 CER の順に最大5回まで同じ設定で再生する。上限超過か配信失敗を観測した時点で、その候補の追加計測を終える。複数回ある場合、採用判定には各回の中央値、最大遅延、ピーク RSS のそれぞれ最も大きい値を使う。

| 構成 | 初回の遅延中央値 | 初回の最大遅延 | 初回のピーク RSS | ASR の破棄区間 | 追加ファイル |
| --- | ---: | ---: | ---: | ---: | ---: |
| ReazonSpeech | 2.96秒 | 9.31秒 | 2.89 GB | 0 | 0 B |
| Haas sherpa fp32 | 3.81秒 | 12.70秒 | 5.23 GB | 2 | 2,505,118,160 B |
| Kizuna int4 | 3.23秒 | 9.11秒 | 3.11 GB | 1 | 380,327,304 B |
| Kizuna int8 | 2.89秒 | 7.83秒 | 3.37 GB | 0 | 663,886,563 B |
| speech.cpp Q4_K | 3.45秒 | 10.36秒 | 2.84 GB | 1 | 398,882,848 B |
| speech.cpp Q8_0 | 3.90秒 | 10.17秒 | 3.15 GB | 1 | 703,363,104 B |

破棄のあった回の遅延は、実際に届いた字幕についての値。いずれも処理中と待機中の音声が ASR 待ち行列の上限30秒に達していた。届いた字幕の最大遅延だけで8秒を超えたため、判定関数の遅延上限に照らして不合格となる。

Kizuna int8 の5回とも ASR が50区間を処理し、英語と簡体字中国語に36枚ずつ字幕を送出した。字幕に使った日本語は、正規化後に同じ設定のオフライン出力と一致した。

| int8 の試行 | 遅延中央値 | 最大遅延 | ピーク RSS |
| ---: | ---: | ---: | ---: |
| 1 | 2,891 ms | 7,828 ms | 3,366,309,888 B |
| 2 | 2,961 ms | 7,985 ms | 3,366,281,216 B |
| 3 | 2,929 ms | 7,843 ms | 3,365,265,408 B |
| 4 | 2,952.5 ms | 7,906 ms | 3,368,742,912 B |
| 5 | 2,984 ms | 7,938 ms | 3,371,597,824 B |

Kizuna int8 のモデルと付属文書10ファイルを `app/models/` 配下に配置し、配布ビルダーの `write_zip` で格納した。モデルなしの ZIP との差は663,888,327 B。判定入力のモデル容量663,886,563 B に ZIP のヘッダーが加わった値で、1 GB の上限内に収まる。

判定関数は int8 を合格とした。Haas fp32 は最大遅延、ピーク RSS、容量が上限を超えた。Kizuna int4 と speech.cpp の両量子化は最大遅延が上限を超えた。

## 回帰と句読点

調整後の各構成で TTS の発話34本と雑音のみ1本を認識した。発話の正解文は正規化後で983文字。過去に誤認識した「今日は植物」「光合成」「原子の数」も確認し、Edge から保存した3つの音声で「今日は植物」が残るかを再試験した。

| 構成 | 発話の CER | 置換・脱落・挿入 | 雑音のみへの挿入 |
| --- | ---: | ---: | ---: |
| ReazonSpeech | 3.97% | 20・17・2 | 2文字 |
| Haas sherpa fp32 | 3.26% | 22・10・0 | 0文字 |
| Kizuna int4 | 4.27% | 32・10・0 | 0文字 |
| Kizuna int8 | 3.66% | 26・10・0 | 0文字 |
| speech.cpp Q4_K | 3.26% | 22・10・0 | 0文字 |
| speech.cpp Q8_0 | 3.56% | 25・10・0 | 0文字 |

「光合成」「原子の数」は全構成で正しく出た。「今日は植物」は元の音声1本と Edge 音声3本すべてで、どの構成でも一致しなかった。Kizuna int4 の発話 CER は現行より高かった。

句読点は採用判定と切り離して観察した。同じ TTS 原文21件で `。、！？` だけを除いて Hy-MT2 に入力すると、英訳8件、中国語訳16件で訳文が変わった。訳文の一致率は変化の頻度を示す。翻訳品質の採点には使っていない。

授業動画の Kizuna int8 出力でも同じ4記号だけを除き、同じ VAD イベントを `TurnAssembler` に渡した。句読点ありでは36 Turn、除いた場合は34 Turn になった。

## 再現手順

[Haas-ja](2026-11-08-haas-eval.md) と [speech.cpp](haas-speech-cpp.md) の固定版を用意する。以下はリポジトリのルートで PowerShell から実行する例。`$haasModels` の下に両 Haas モデルのディレクトリを置き、`$speechDir` に GGUF と展開した `speech/speech.exe` を置く。`$ref` は #168 で確定した JSON に設定する。出力先には未作成のディレクトリを指定する。回帰には `tests/fixtures/ja_ext/index.json` の生成済み音声と、[前回の ASR 比較](2026-10-21-asr-family-comparison.md) で保存した `artifacts/asr-candidates/dataset.json` と Edge 音声3件を使う。

```powershell
$python = '.\.venv\Scripts\python.exe'
$base = Join-Path $env:LOCALAPPDATA 'LinguaBridge/evaluation'
$out = Join-Path $base 'asr-adoption'
$ref = Join-Path $base 'issue-168/ref.json'
$haasModels = 'C:\asr-models'
$speechDir = 'C:\asr-eval'

& $python -m scripts.lesson_asr_benchmark accuracy --out $out --ref $ref `
    --haas-models $haasModels --speech-cpp-dir $speechDir
& $python -m scripts.lesson_asr_benchmark package-sizes --out "$out/package-sizes.json" `
    --haas-models $haasModels --speech-cpp-dir $speechDir

foreach ($name in 'reazon','haas-int4','haas-int8','speech-q4_k','speech-q8_0','haas-fp32') {
    & $python -m scripts.lesson_asr_benchmark replay "$out/$name-tuned.yaml" --out "$out/$name.replay.1.json"
}
foreach ($name in 'reazon','haas-fp32','haas-int4','haas-int8','speech-q4_k','speech-q8_0') {
    & $python -m scripts.lesson_asr_benchmark regression "$out/$name-tuned.yaml" --out "$out/$name.regression.json"
}
& $python -m scripts.lesson_asr_benchmark punctuation --out "$out/punctuation.json"
foreach ($trial in 2..5) {
    & $python -m scripts.lesson_asr_benchmark replay "$out/haas-int8-tuned.yaml" --out "$out/haas-int8.replay.$trial.json"
}
& $python -m scripts.lesson_asr_benchmark summarize $out --ref $ref --out "$out/measurements.json"
& $python -m scripts.asr_adoption "$out/measurements.json"
```
