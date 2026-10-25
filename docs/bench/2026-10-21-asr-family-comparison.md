# ASR候補と配布モデルの選択

2026年10月21日、開発機のRyzen 7 8840U、Windows、CPUのみで比較した。
今回の候補では、既定のReazonSpeechを置き換えて「今日は植物」の誤認識と処理速度を両立するモデルは見つからなかった。
既定ASRとスレッド設定は維持する。HuBERTは評価用ディレクトリへ分離し、配布モデルには登録していない。

## 比較したモデル

既定のReazonSpeech K2 v2は既にZipformer transducerである。[開発元の説明](https://research.reazon.jp/blog/2024-08-01-ReazonSpeech.html) にモデル構成と量子化仕様がある。
Sherpaの日本語Zipformer配布も [同じReazonモデル由来](https://github.com/k2-fsa/sherpa/blob/master/docs/source/onnx/pretrained_models/offline-transducer/zipformer-transducer-models.rst) のため、別モデルとして数えていない。

HuBERTはReazon公式の [文字CTC版](https://huggingface.co/reazon-research/japanese-hubert-base-k2-rs35kh) と
[BPE版](https://huggingface.co/reazon-research/japanese-hubert-base-k2-rs35kh-bpe) を試した。
事前学習の特徴抽出モデルや、平仮名・音素だけを出すモデルは、漢字字幕用の代替モデルと混同しなかった。
文字版リビジョンは `30acf6f643914538edb15fc708f670a520478ea4`、BPE版は `a6660b26be72cf66cf18519807ad07866d55eb3e`。
取得ファイルのSHA256、サイズ、固定URLは [models.json](../../artifacts/asr-candidates/models.json) に保存した。

## 測定条件と結果

既存TTS 10文と、実Edge送出PCMを本番Silero/VADで切った3発話を使った。
速度は後者3発話を各5回、候補の順を循環させて測った。表の中央値と範囲は合計15観測である。
モデルロードとファイル読込みは計測外。正規化、padding、推論結果の具体化、decodeを含む。
各モデルはwarmup済みで、同時に一つだけロードした。2/4/8スレッドを測定外で各3回試し、Kotobaは既定0も試した。
HuBERTは公式推奨の前後0.5秒paddingを使い、ZipformerとKotobaは製品の実ASR実装を使った。

| 候補 | 測定スレッド | ASR中央値 | 最小〜最大 | 10文のCER | モデル容量 |
|---|---:|---:|---:|---:|---:|
| ReazonSpeech K2 v2、Zipformer、現行int8/fp32 | 2 | 99.2ms | 88.7〜114.8ms | 2.18% | 172.14MB |
| HuBERT文字版、PyTorch FP32 | 8 | 244.0ms | 200.1〜281.5ms | 7.64% | 393.68MB |
| HuBERT BPE版、PyTorch FP32 | 8 | 235.0ms | 207.0〜285.9ms | 7.64% | 386.89MB |
| HuBERT文字版、ONNX INT8試作 | 8 | 122.5ms | 100.6〜172.8ms | 8.36% | 約126.03MB |
| Kotoba Whisper v2.0、CTranslate2 int8実行 | 8 | 5,138.2ms | 4,937.8〜5,437.8ms | 2.55% | 1,516.48MB |

MBは10進。既存モデルは配布対象ファイル一式、HuBERT FP32は取得snapshot一式のディスクサイズ。
ONNX試作はINT8重み125,949,080 bytesとprocessor/tokenizer設定79,930 bytesの合計で、製品パッケージとしての採用はしていない。
Kotobaのディスク上の重みと、実行時に選ぶint8計算は別である。
Reazonの同梱物には既存目録にある比較用int8 decoder約2.96MBも含む。

ONNX追加測定は別の交互測定で行った。同時に測ったZipformerは99.6ms、範囲88.7〜111.9msだった。
表の2/4スレッドの微差から、Zipformerの製品既定4スレッドを2へ変える判断はしていない。

CERは既存 `scripts/text_metrics.py` のNFKC・空白/記号除去と編集距離で集計した。
漢数字と算用数字は同一化しないため、「四十二」と「42」の差も誤りに数える。
10文は単一TTS音声の小さな集合であり、授業全般の精度評価ではない。

## 「今日は植物」の結果

| 入力 | Zipformer | HuBERT文字 | HuBERT BPE | HuBERT ONNX INT8 | Kotoba |
|---|---|---|---|---|---|
| 元の01.wav | 共和植物 | 共和植物 | 共和植物 | 共和植物 | 今日は植物 |
| Edge送出/VAD音声、480msプリロール | 共和植物 | 共和植物 | 共和植物 | 共和植物 | 今日は植物 |

HuBERT文字版は「教科書の重にページ」、BPE版は「教科書の四重二ページ」等も観測した。
ONNX化はMatMul/Gemmの動的INT8量子化で、Convは量子化していない。
サイズは減ったが、この入力の誤認識を解消せず、10文のCERも改善しなかった。
速度を優先する通常運用はZipformerを維持する。既知文の正しい認識を優先する場合だけ既存 `config.accuracy.yaml` のKotobaを選ぶ。

## 数値の確認範囲

全36 worker実行は終了コード0で、各入力から非空の認識結果を取得した。
比較ツールは完全一致の正例、既知の「共和植物」誤り、出力割当ての入替えを検出する対照確認も通した。
測定中の利用可能メモリーは最低約3.14GB。入力I/Oを計測前に終え、重い別モデルや実ZIPビルドを同時実行していない。

CPU処理の場所は測定外で確認した。KotobaはcProfileで約4.72/4.87秒がencoder、HuBERT FP32はlinear/convが主だった。
Zipformerのpybind内部はcProfileの時間集計から欠落するため、別の
[native probe](../../artifacts/asr-candidates/zipformer-native-probe.json) でdecode_streamが約93ms、準備が約6msであることを確認した。
CPU時間/壁時間の中央値はZipformer約2.9、HuBERT ONNX約8.1だった。ASR単体の数値から、翻訳と同時実行した際の字幕遅延を推定しない。

既存の実Edge試験では、最初の文の発話確定後の内部 `delay_ms` はZipformerで英547/中812ms、Kotobaで英5,937〜6,094/中6,390〜6,594msだった。
前者の原文は誤認識、後者は正解である。VAD確定前の待ち時間は含まない。
これは今回の繰返しASR測定と同一条件の速度比較ではなく、実配信経路の参考観測である。
[抽出した実配信値](../../artifacts/asr-candidates/prior-end-to-end.json) と [既存の検証記録](../../.agents/skills/verify/references/asr-opening-fix-proof.md) を参照。
HuBERTは製品pipelineへ統合しておらず、実ブラウザーから翻訳までの成功は主張しない。

実行環境は製品側がsherpa-onnx 1.13.6 / faster-whisper 1.2.1 / numpy 2.5.1。
評価側はTorch 2.14.1+cpu / Transformers 4.57.6 / ONNX Runtime 1.31.0 / numpy 2.5.4。
配布先PC、教室騒音、実話者、長時間授業は未測定。

## 再現用の記録

- [測定契約](../../artifacts/asr-candidates/measurement-contract.md) と [入力・SHA256](../../artifacts/asr-candidates/dataset.json)。
- [最初の4候補の全結果](../../artifacts/asr-candidates/summary.json) と [ONNX追加測定](../../artifacts/asr-candidates/onnx-comparison/summary.json)。
- [compare.py](../../artifacts/asr-candidates/compare.py)、[asr_worker.py](../../artifacts/asr-candidates/asr_worker.py)、[summarize.py](../../artifacts/asr-candidates/summarize.py)。
- [ONNX変換](../../artifacts/asr-candidates/export_hubert_onnx.py)、[ONNX比較](../../artifacts/asr-candidates/compare_onnx.py)、[ONNX集計](../../artifacts/asr-candidates/summarize_onnx.py)。

評価環境と新規重みはOneDrive外の `%LOCALAPPDATA%/LinguaBridge/asr-eval` と `asr-eval-models` にある。
取得・変換・比較用の依存を製品 `.venv`、`requirements.txt`、モデル目録へ追加していない。
配布の選択方法は [READMEのビルド手順](../../README.md)、実ZIPの検証は [モデル選択の検証記録](2026-10-21-model-bundle-build.md) に記載した。
