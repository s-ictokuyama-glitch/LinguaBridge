# Live Translation 実エンジン受け入れ確認（#111）

先生・生徒・サーバーPCの操作者に製品名を Live Translation と表示し、通常の画面と想定内のエラーから翻訳モデルの実名を除くことを確認した。

実施日は2026-11-02（JST）。start.batと欠損の専用ステージは `8c4c740`。実字幕はその後のMTエラー一般名化修正も含む作業ツリーで再実行した。実行時の全server/web/configのSHA256は `artifacts/verify/20261102-acceptance111-final/instance.json` の `sources` に保存した。実エンジンは Silero VAD、ReazonSpeech K2 v2、Hy-MT2。ブラウザーは実Edge 155.0.4283.45。ASR・MT・HTTP・WebSocketを置換していない。物理マイクの代わりに既存の日本語検証WAVをEdgeのファイルマイクへ入力した。

| 受け入れ条件 | 結果と証拠 |
| --- | --- |
| `start.bat` の起動表示 | 合格。専用ステージの実 `start.bat` を起動。`Live Translation サーバー起動` と `server.mt:翻訳モデルを読み込み中／準備完了` を確認。翻訳実名なし。`artifacts/verify/20261102-acceptance111-start/normal-start.stdout.log`、`normal-start.stderr.log`。 |
| 字幕・先生・接続ヘルプ | 合格。実Edgeで3ページのtitle、見出し、本文、先生の読み上げ見出し、展開した診断案内を確認。製品名はLive Translation、本文に翻訳実名なし。`pages-result.json`、各 `*.aria.txt`・`*.png`、`pages-trace.zip`。 |
| `start.bat --diagnose` | 合格。実batを実行し終了0。HTTP、モデル準備、ページ取得、証明書整合、提供中証明書、HTTPS死活確認が確認済み。`diagnose.stdout.log`、`diagnose.stderr.log`。別端末とFWの実効許可は診断どおり未確認。 |
| モデル一時欠損と復元 | 合格。隔離models内のGGUFハードリンクだけを一時退避し実batを起動。終了1、「翻訳モデルが見つからない」、modelsまでのパス、展開し直し案内を確認。実名なし。復元前後と共用原本のSHA256一致。`missing.stdout.log`、`missing.stderr.log`、`missing-result.json`。共用原本は移動していない。 |
| 実翻訳の字幕配信 | 合格。先生と独立したen/zh生徒へ3件をseq順に配信。教科書42ページの原文に対し `Please open page 42 of the textbook.` と `请打开教科书的第四十二页。` を表示。終了表示、記録ONの発話だけの保存、ブラウザー例外なしも確認。`artifacts/verify/20261102-acceptance111-final/audio-recording/result.json`、同trace、画像、`websocket-frames.json`、`recording-evidence/`。 |
| Issueへの結果記録 | [受け入れ結果を記録済み](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/111#issuecomment-6560396290)。 |

## #106 Further Notes の残存表示

- 音声認識モデル名は実起動ログで `server.asr.sherpa_engine` と `reazonspeech-k2-v2` のまま残る。これは対象外のASR表示である。
- 既存の共用証明書は `CN=LinguaBridge` のまま。採用IPとのSAN不一致を正しく警告した。共用証明書・鍵は変更せず、専用ステージのコピーだけに新規発行した証明書は `CN=Live Translation`。新旧CNによる互換判定は行っていない。
- ファイアウォール規則名は `scripts/os_setup.ps1` の `LinguaBridge TCP $p` を維持する。ソースを確認したが、初回UACと規則作成は実行していない。
- 配布物内部のモデル名・ライセンスは共用 `models/manifest.json` で実名とライセンス情報の残存を確認した。保存記録の `translations.en.engine` と `translations.zh.engine` も `hy-mt2` のままで、画面表示とは分離されている。今回配布zip自体は生成していない。
- 想定外例外のトレースバックは本試験では意図的に発生させていない。実行した起動・正常配信・欠損の範囲に想定外例外はない。
- 保存先と内部識別子の `LinguaBridge` は意図した残存。確認したページの製品名・翻訳ログ・欠損メッセージに対象外の漏れは認めなかった。この結論は実施した入口に限定する。

## 隔離と後片付け

通常サーバーは専用ポート・専用データで起動し、`20261102-acceptance111-final/cleanup.json` の停止・scratch削除を確認した。start.batは統合checkoutから作った専用コピーで実行し、既存venvを読み取り参照した。依存導入、モデル取得、UAC、OS設定変更、共用証明書変更は実行していない。start所有プロセスも生成時刻とコマンドを照合し停止済み。`20261102-acceptance111-start/cleanup.json` を参照。

最終runではWebSocketへ実送信したPCM16LEも `audio-recording/sent-pcm16le.raw` に保存した。1,363,200 bytes、SHA256は `d99f3da5f57383b8570fc5098300f017ce7c2645371fec9cc5b924563be896b5`。この再実行後のpipeline遅延ロード例外処理の追加修正は本実音声runに含まれず、親エージェントの回帰試験で別途確認する。

最終的な例外処理は、モデル不要の起動・翻訳境界テストを含む87件で確認した。想定内の形式不正は起動時と遅延ロード時の両方でトレースバックを出さず、想定外例外は従来どおり出す。追加前の関連回帰258件も全件成功した。

1件目の「今日は植物」は既定ASRで「共和植物」となり、英語にも日本語が残った。これは既存の [#89](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/89) と同じ観測である。今回の合格は製品名・モデル名の表示と実翻訳配信に限り、認識・翻訳の全面的な正しさを意味しない。

学校LAN、別端末HTTPS信頼、物理収音、長時間授業、翻訳品質の網羅評価は未検証。検証用音声以外を録音していない。ローカル証拠には参加コードや機器情報を含むため、公開時は本文の結果だけを転記し、秘密鍵を添付しない。

## 再実行

`.agents/skills/verify/scripts/instance.py launch` で作成しdoctorが成功する新しい専用runに対し、次を実行する。実在モデルは起動ヘルパーへ渡す。qa-toolsとfixturesが別checkoutにある場合も絶対パスで指定できる。

```powershell
python scripts/qa_live_translation_acceptance.py --run-dir artifacts/verify/<new-run> --qa-tools <playwright-directory> --fixture-dir <japanese-wav-directory>
```

スクリプトは `01.wav`、`03.wav`、`07.wav` を使用する。先生・en生徒・zh生徒の実ブラウザー、字幕、録音切替、終了、記録ファイルを照合する。失敗時もtrace・例外・送出PCMを保存する。サーバーの後片付けは同じrunへ `instance.py cleanup` を実行する。
