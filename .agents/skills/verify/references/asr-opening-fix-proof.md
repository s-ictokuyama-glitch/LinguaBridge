# 「今日は植物」の誤認識調査と再検証

この記録の `artifacts/`、任意設定、補助QA・回帰テストは、この開発機にのみ存在する未追跡ファイルを含む。スキル単体のPRには同梱せず、別のcheckoutでは証拠リンクや再実行スクリプトを利用できない。ファイルが無い場合は本記録を実行証拠として使わず、SKILL.mdのlaunchから新しい証拠を作る。

2026年10月21日、このWindows checkoutで実施した。
既定ReazonSpeechの誤認識を再現し、任意の `config.accuracy.yaml` にKotobaと `vad.pre_roll_ms: 480` を設定した。
通常起動のReazonSpeech設定は維持した。この音源への改善であり、全発話の精度向上を示すものではない。

## 原因を絞った証拠

元音源は `tests/fixtures/ja/01.wav`、正解は「今日は植物の光合成について勉強します。」。
SHA256は `c5b1e8d4ac37153fe60143686395cffc0187312671bb528e5788526e67312d84`。

| 実験 | 観測 |
|---|---|
| 既定ReazonSpeechへ元WAVを直接入力 | 「共和植物…」 |
| 実Edge送出PCM→Silero→既定ReazonSpeech | 同じ誤認識 |
| greedy/beam探索、lead-in、探索幅、hotwordを変更 | この文の誤りは残った。強いhotwordは別文を壊した |
| encoder/joinerをFP32へ変更、全4組合せ | 同じ誤認識 |
| Kotobaへ元WAVを入力 | 「今日は植物…」 |
| Kotobaへ実Edge送出PCM全体を入力 | 正解 |
| 実Edge送出PCM→Silero、pre-roll 240ms→Kotoba | 「共和植物…」。モデル変更だけでは不十分 |
| 同じ切出しに末尾320/640ms無音を追加 | 誤認識が残った |
| 同じ切出しに先頭320ms無音を追加 | 正解 |
| pre-roll 480/800ms→Kotoba | 双方で正解 |
| pre-roll 480/800ms→ReazonSpeech | 双方で誤認識 |

ReazonSpeechのモデル・デコードがこの音声で誤った文字列を選ぶ。採用時の
`docs/bench/2026-08-24-asr-gate.json` にも同じ誤りがあるため、今回のブラウザー配信変更による回帰ではない。
Kotobaでも入力先頭の文脈により結果が変わるため、モデル切替とプリロール延長を組み合わせた。
単語の事後置換、特定文のプロンプト、信頼度を推測した自動切替は加えていない。
内部のどの重みが誤選択を生んだかまでは特定していない。

詳細は [分離診断](../../../../artifacts/asr-diagnosis/direct/findings.md)、
[精度形式の比較](../../../../artifacts/asr-diagnosis/fp32/precision-comparison.json)、
[Kotobaの切出し比較](../../../../artifacts/asr-diagnosis/kotoba-segmentation/results.json)、
[Reazonの切出し比較](../../../../artifacts/asr-diagnosis/kotoba-segmentation/reazon.json) に残した。
FP32比較には公式ReazonSpeechリポジトリのリビジョン `291488c8151be24d7da4bf7af26e533fad96e407` のファイルを使用し、SHA256を照合した。
調査用FP32コピーは比較後に削除した。[model-sources.json](../../../../artifacts/asr-diagnosis/fp32/model-sources.json) に取得URL・サイズ・SHA256を保存した。運用モデルは変更していない。

## 失敗と成功を同じ画面で確認

- [baseline-01](../../../../artifacts/verify/20261021-asr-baseline-01/audio-recording/result.json) は既定ReazonSpeechで失敗。「共和植物…」を実Edgeの字幕で観測した。
- [kotoba-01](../../../../artifacts/verify/20261021-asr-kotoba-01/audio-recording/result.json) はKotoba＋240msで失敗。元WAVの直接認識成功を、配信成功の代用にはしなかった。
- [kotoba-02](../../../../artifacts/verify/20261021-asr-kotoba-02/audio-recording/result.json) はKotoba＋480msで成功。
- [kotoba-03](../../../../artifacts/verify/20261021-asr-kotoba-03/audio-recording/result.json) も新規サーバー・ブラウザーcontextで成功。

実Edge 155.0.4283.45、製品CLI、実HTTP/WS、48kHzの `getUserMedia`、AudioWorkletの16kHz PCM変換、Silero、Kotoba、Hy-MT2を通した。
物理マイク入力だけを検証用WAVのEdgeファイルキャプチャで置き換えた。
アプリのmedia API・pipeline・ASR・翻訳をfakeへ差し替えていない。

02の保存結果は以下の通り。画面のseq 1とJSONL・ja/en/zh Markdownを照合した。

| 言語 | 結果 |
|---|---|
| ja | 今日は植物の光合成について勉強します |
| en | Today, we will study about plant photosynthesis. |
| zh | 今天我们将学习关于植物的光合作用。 |

「教科書の42ページ…」「質問がある人は…」もen/zhにseq 2/3の順序で配信された。
記録ONはseq 1だけ、OFFのseq 2/3は保存されなかった。終了前のファイル書込みなし、終了時の4ファイル、画面との一致、生徒情報を含まないschema、ページエラー0件をassertした。
画像、ARIA、WebSocketフレーム、送出PCM、Playwright traceは各runの `audio-recording/`、保存ファイルは `recording-evidence/` にある。

02/03のseq 1ではASR処理が5,500/5,657msだった。アプリの発話確定後の `delay_ms` は英語5,937/6,094ms、中国語6,390/6,594ms。
これはVADが発話を確定するまでの待ち時間を含まない。処理時間が増えるため、既定エンジンは変更していない。
同じ1文を2回通した観測値であり、速度比較ベンチマークではない。
[live-summary.json](../../../../artifacts/asr-diagnosis/live-summary.json) にフレーム数・遅延・終了後の照合結果を保存した。
両runのdoctorを再実行してからcleanupし、所有サーバー停止、専用tmp削除、記録4ファイルのSHA256一致、traceの残存を確認した。

## 再実行

既存モデルの実パスとLANアドレスを確認し、SKILL.mdのlaunchに `--config-override config.accuracy.yaml` を加える。
本PCの既存モデルは `C:\Users\esesm\AppData\Local\LinguaBridge\models`。
Kotobaは標準配布モデルには含まれないため、モデルが無い環境では先に配置が必要。

実際に使った [drive_audio_recording.py](../../../../artifacts/verify/20261021-asr-kotoba-02/drive_audio_recording.py) を新規runへコピーする。
スクリプトは自身の親ディレクトリをrunとしてdoctorを実行する。新しいrunをlaunch後、そのコピーを既存 `.venv` で実行し、最後に必ずcleanupする。
入力の01/03/07.wavが無ければ `scripts/make_fixture_audio.ps1` で準備する。

回帰テストは次の通り。実モデルと録音を指定した今回の実行は **5 passed、skipなし**。

```powershell
$env:LINGUABRIDGE_TEST_MODELS_DIR = 'C:\Users\esesm\AppData\Local\LinguaBridge\models'
$env:LINGUABRIDGE_TEST_BROWSER_AUDIO = (Resolve-Path 'artifacts/verify/20261021-asr-kotoba-01/audio-recording/teacher-sent.wav').Path
.venv/Scripts/python.exe -m pytest tests/integration/test_asr_classroom_opening.py tests/unit/test_verify_instance_config.py
```

モデル・生成WAVが無い環境はskip、録音の環境変数が未指定なら録音回帰だけskipする。
元音源ではReazon、録音後ではKotoba＋240msで実際に赤を確認してから修正した。
[vad-red.log](../../../../artifacts/asr-diagnosis/profile-tests/vad-red.log)、
[vad-green.xml](../../../../artifacts/asr-diagnosis/profile-tests/vad-green.xml) に残してある。

物理マイク、教室騒音、別端末LAN/HTTPS、長時間授業、多話者・全授業語彙の精度は未検証。
