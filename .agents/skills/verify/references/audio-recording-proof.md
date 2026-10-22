# 音声配信と記録保存の検証結果

この記録の `artifacts/`、任意設定、補助QA・回帰テストは、この開発機にのみ存在する未追跡ファイルを含む。スキル単体のPRには同梱せず、別のcheckoutでは証拠リンクや再実行スクリプトを利用できない。ファイルが無い場合は本記録を実行証拠として使わず、SKILL.mdのlaunchから新しい証拠を作る。

2026年10月21日、作成した `verify` スキルのlaunch、doctor、Drive、Evidence、cleanupに従って開発機で実施した。
配信と保存の機能検証は成功した。認識・翻訳の文面には下記の課題が残る。

## 実行した経路

製品CLIから専用インスタンスを起動した。Edge 155.0.4283.45の実 `getUserMedia`、AudioWorklet、WebSocket、Silero VAD、ReazonSpeech K2 v2、Hy-MT2を通した。
ASR、翻訳、HTTP、WebSocket、字幕表示をfakeへ置き換えていない。内部stateやpipelineへ字幕・PCMを注入していない。

物理マイクの代わりに、既存のWindows日本語TTSフィクスチャ3件をWAVにまとめてEdgeのファイルキャプチャへ与えた。
この入力方式は [Chromiumの `use-file-for-fake-audio-capture` 定義](https://chromium.googlesource.com/chromium/src.git/+/cc79060bcce11b0cb6fafa673a2a20dcb12bd077/media/base/media_switches.cc) に従う。
48kHzのブラウザー収音からアプリのWorkletを通り、16kHz PCMのバイナリフレームが送られることを観測した。
物理マイクの音質、実授業の雑音や学校LANはこの試験の対象外。

実行ディレクトリは [20261021-audio-recording-01](../../../../artifacts/verify/20261021-audio-recording-01/)。
起動、実行、終了に使ったコマンドは次のとおり。

```powershell
.venv/Scripts/python.exe .agents/skills/verify/scripts/instance.py launch --run-dir artifacts/verify/20261021-audio-recording-01 --models-dir C:\Users\esesm\AppData\Local\LinguaBridge\models --advertise-ip 192.168.1.6
.venv/Scripts/python.exe artifacts/verify/20261021-audio-recording-01/drive_audio_recording.py
.venv/Scripts/python.exe .agents/skills/verify/scripts/instance.py doctor --run-dir artifacts/verify/20261021-audio-recording-01
.venv/Scripts/python.exe .agents/skills/verify/scripts/instance.py cleanup --run-dir artifacts/verify/20261021-audio-recording-01
```

WindowsのTCP所有確認とEdge起動に必要な通常ユーザー権限で実行した。
再実行時は新しいrunをlaunchし、[実行スクリプト](../../../../artifacts/verify/20261021-audio-recording-01/drive_audio_recording.py) をそのrun直下へコピーして実行する。
スクリプトは自分の配置先をrunとして扱う。完了・失敗の両方でcleanupを実行する。

## 配信と保存の照合

先生1context、英語・中国語の生徒各1contextを使用した。
各生徒が受信した字幕のseqは `[1, 2, 3]`。画面と実WS受信フレームの双方で順序と欠落・重複なしを照合した。

| 発話 | 入力フィクスチャ | 記録状態 | 終了時JSONL |
| --- | --- | --- | --- |
| seq 1 | `01.wav`、植物の光合成 | 既定OFF | 含まれない |
| seq 2 | `03.wav`、教科書の42ページ | ON | 原文とen/zh訳文を保存 |
| seq 3 | `07.wav`、質問と挙手 | 再びOFF | 含まれない |

記録ON/OFFに先生・生徒両画面のインジケーターが追従した。
授業終了前にファイルが作られないことと、終了後にJSONLが1件だけ保存されることを確認した。
JSONLのキーは発話、seq、時刻、処理時間、訳文に限定され、生徒のIPや識別子のフィールドは無かった。
日本語・英語・中国語のMarkdownは各1発話で、保存したseq 2の画面・JSONLと一致した。
終了時には先生が「終了」、生徒がそれぞれの言語の授業終了表示になった。ページエラーは0件。

保存した内容は次のとおり。

- 原文は「教科書の四十二ページを開いてください」。
- 英語は「Please open page 42 of the textbook.」。
- 中国語は「请打开教科书的第四十二页。」。

## 証拠と終了処理

- [result.json](../../../../artifacts/verify/20261021-audio-recording-01/audio-recording/result.json) はpass。入力音源のSHA-256、操作履歴、全段階の原文・訳文、記録内容を持つ。
- [websocket-frames.json](../../../../artifacts/verify/20261021-audio-recording-01/audio-recording/websocket-frames.json) に実送受信の観測を保存した。バイナリはサイズだけを記録し、内容はテスト入力WAVを参照する。
- [記録中の先生画面](../../../../artifacts/verify/20261021-audio-recording-01/audio-recording/02-caption-teacher.png) と [英語生徒画面](../../../../artifacts/verify/20261021-audio-recording-01/audio-recording/02-caption-en.png) で原文・訳文・記録表示を照合した。他の段階にも画像とARIAがある。
- [保存JSONL](../../../../artifacts/verify/20261021-audio-recording-01/recording-evidence/transcript.jsonl)、[日本語Markdown](../../../../artifacts/verify/20261021-audio-recording-01/recording-evidence/transcript.ja.md)、[英語Markdown](../../../../artifacts/verify/20261021-audio-recording-01/recording-evidence/transcript.en.md)、[中国語Markdown](../../../../artifacts/verify/20261021-audio-recording-01/recording-evidence/transcript.zh.md) を専用tmpから証拠領域へ退避した。
- `teacher-trace.zip`、`en-trace.zip`、`zh-trace.zip` に操作前から終了までのtraceを保存した。
- [doctor-after-drive.json](../../../../artifacts/verify/20261021-audio-recording-01/doctor-after-drive.json) で操作後も所有PID・ポート・設定・ソース一致を確認した。
- [post-cleanup.json](../../../../artifacts/verify/20261021-audio-recording-01/post-cleanup.json) で実サーバー停止、専用tmp削除、4記録ファイルと3traceの残存を確認した。退避ファイルのSHA-256も終了後に再照合した。

## 残る観測事項

seq 1の元文は「今日は植物の光合成について勉強します。」だが、ASR結果は「共和植物の光合成について勉強します」になった。
後続の調査で任意Kotoba＋480msプロファイルによる改善を検証した。既定ReazonSpeechにはこの誤りが残る。[改善の検証記録](asr-opening-fix-proof.md) を参照する。
英語字幕も `I will study the photosynthesis of共和 plants.` と表示され、日本語文字が残った。
この反例は [review.json](../../../../artifacts/verify/20261021-audio-recording-01/review.json) に記録した。認識・翻訳精度を全面的な合格とは扱わない。
seq 2とseq 3は、元音声の指示内容に対応した日英中の字幕を確認した。

生徒画面のスクリーンショットでは参加フォームが字幕画面の上部に残る表示も観測した。
`web/student.css` は `#join-screen` に `display: flex` を指定し、`#caption-screen[hidden]` と異なり参加画面のhidden専用ルールを持たない。
この表示の修正は行っていない。今回の配信・保存assertはこの状態でも成立した。

再接続・50件履歴・途中参加者の記録表示、記録OFFだけで終了する別授業、長時間授業、学校LAN、別端末HTTPS、物理マイク、意味精度の網羅評価は今回追加検証していない。
製品コードと既存QA台帳は変更していない。
