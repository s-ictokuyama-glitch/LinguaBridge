# ビルド時のモデル選択の検証

2026年10月21日。ビルドCLIで同梱モデルを選べるようにし、2構成の実ZIPを作って内容を検査した。
モデル名の固定リストから、有効設定を起点とする単一の `BundlePlan` へ変更した。
通常起動のASR、翻訳、スレッド設定は変更していない。

## 選択規則

- 既定は有効なASR/MTのみ。現在の `config.yaml` ではReazonSpeech＋Hy-MT2。
- `--include-model NAME` は予備モデルを追加する。NLLBの追加はtokenizerも含む。
- `--exclude-model NAME` は有効モデルや依存の除外なら、作業領域更新前に拒否する。
- `--config-override PATH` は部分設定を重ねる。設定のモデルと同梱物を別々に選んで不整合にしない。
- `--plan` は取得・ビルドをせず、ローカルファイルとモデルpayload bytesを表示する。
- 設定、コピー、manifest、versionは同じ計画から生成する。明示設定の欠損、外部モデルパス、モデル内リンクも拒否する。

予備NLLBを含む旧4モデルの計画は1,974,764,039 bytes。
新しい既定2モデルは1,305,221,672 bytes。
モデルpayloadで669,542,367 bytes、約669.5MBを減らした。モデルはZIP_STOREDなので、圧縮率の見込み値ではない。
Pythonと依存ライブラリは今回の選択単位に含めず、実エンジンの互換性確認を維持した。

## 実CLIと生成ZIP

[8つのCLIケース](../../artifacts/model-bundles-design/cli/result.json) は全件が期待した終了コードになった。
既定、NLLB追加、Kotoba上書き、未使用モデル除外は成功。
有効ASR除外、NLLB依存の除外、未知名、存在しない上書きファイルは失敗した。
単体テストではplanから通信・書込み・buildを呼んだら失敗させる対照も通した。

| 実ビルド | 同梱モデル | モデルpayload | ZIP全体 |
|---|---|---:|---:|
| 既定 | ReazonSpeech、Hy-MT2 | 1,305,221,672 bytes | 1,517,499,713 bytes |
| `mt.engine: nllb` の上書き | ReazonSpeech、NLLB、tokenizer | 841,683,591 bytes | 1,053,965,030 bytes |

2構成とも、同梱Pythonによる全166ネイティブモジュールのDLL依存検査、実native import、9種CPU backendを持つllama.cppのCPU判定検査に成功した。
生成した設定で実ASRと選択MTをfactoryから構築・warmupし、選択MTの推論出力を確認した。
既定のHy-MT2は「こんにちは。」に `Hello.` を返した。
NLLB構成は `Hey, hey, hey, hey.` を返した。ロード・推論の試験は通ったが、この訳は反復があり、意味精度の合格とは扱わない。
NLLBへの切替を容量だけで推奨する結果ではない。既定MTはHy-MT2を維持する。
[開発環境との分離確認](../../artifacts/model-bundles-design/nllb-quality-isolation.json) でも同じ反復を再現し、[Issue #53](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/53) に切り出した。
サーバー配線の別試験ではfake ASR/MTの一時設定で `/ready` 200を確認した。このready試験を実モデル品質の証明には使っていない。

ZIP内の全ファイルCRC、モデル相対パス集合、manifest、version、合計bytes、設定のSHA256一致を検査した。
既定ZIPにはNLLB/Kotoba/HuBERTを、NLLB構成にはHy-MT2/Kotoba/HuBERTを収録していない。評価用Torchも混入していない。
物理マイク、教室LAN、配布先PCでの初回起動はこのビルド試験の対象外。

- [ビルドとZIP検査の実行スクリプト](../../artifacts/model-bundles-design/verify_builds.py)。
- [ZIPの実パス・SHA256・収録ファイル](../../artifacts/model-bundles-design/build-results.json)。
- [既定ビルドの全ログ](../../artifacts/model-bundles-design/build-default.log)。
- [NLLB構成の全ログ](../../artifacts/model-bundles-design/build-nllb-active.log)。
- [最終単体テスト38件の結果](../../artifacts/model-bundles-design/unit-38.xml)。

## 検証中に修正した点

独立レビューで、Windowsの改行変換により `version.config_sha256` が実ファイルと違うことが分かった。
実ファイルbytesとの比較で失敗を再現し、設定をUTF-8 bytesとして保存する形に直した。
生成ZIPの設定bytesでもhash一致を確認した。

最初の実ビルドは、Windows PowerShellが親のPowerShell 7モジュールパスを継承し、署名検査モジュールをロードできず停止した。
同じ署名付きPythonを使う [環境差の比較](../../artifacts/model-bundles-design/signature-environment.json) で原因を確認した。
署名確認の子プロセスだけ `PSModulePath` の継承を外し、Windows PowerShellの既定モジュールを使うようにした。
Python Software FoundationとMicrosoftの有効な署名の確認条件は維持した。
失敗した [最初のビルドログ](../../artifacts/model-bundles-design/build-default-attempt1.log) も保存した。

追加の境界確認で、予備モデルのsnapshotにREADME等だけが残り、重みや設定が欠けた場合も拒否するようにした。
Kotoba、Whisper small、NLLBの `model.bin` / `config.json` 欠損6ケースを赤で再現した後、38件の単体テストが成功した。
この事前拒否条件を追加した後も、[最新の計画と実ZIPの再照合](../../artifacts/model-bundles-design/post-guard-zip-check.json) で、設定bytes・収録ファイル・実行コード・ZIPのSHA256が一致した。

設計ではModel the Domain原則に従い、設定・ファイル・容量の選択を `BundlePlan` に集めた。
Build the Lever原則に従い、モデル比較と実ZIP検査を再実行できるスクリプトにした。
Separate Before Serializing Shared State原則に従い、実装者は専用worktreeを使い、CPU推論と実ビルドを順番に実行した。
比較の判断は [ASR候補の記録](2026-10-21-asr-family-comparison.md)、操作は [README](../../README.md) を参照。
