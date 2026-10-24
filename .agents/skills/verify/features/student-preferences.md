# 生徒の表示設定

生徒は原文の表示、文字サイズ、翻訳言語を変えられる。原文と文字サイズは再読み込み後も保持される。

## Sub-features

- `display-original` は原文併記を切り替える。
- `display-size` は小・中・大の3段階を切り替える。
- `display-persist` は再読み込み後にも原文と文字サイズを保持する。
- `display-language` は参加中の言語を切り替え、案内文も追従する。

## How to get to it (user POV)

生徒として参加し、字幕画面の補助行にある「表示設定」(`#settings-btn`)で下からのシート(`#settings-sheet`)を開き、原文チェックボックス、文字サイズボタン、言語selectを操作する。操作後は `#settings-close` で閉じる。

## Driving it with Playwright

Preconditions:

- 未開始の専用runがready。先生・生徒contextに既存のlocalStorageがない。

- **大・原文非表示・保持・言語。** `.venv/Scripts/python.exe .agents/skills/verify/scripts/student_preferences.py --run-dir "$verifyRun"` を実行する。L選択と原文OFF、reloadと再参加、zh変更をtraceと結果で確認する。
- **全サイズ。** 同じ実サーバーの別セッションで `student.locator('#settings-btn').click()` でシートを開いてから `student.locator('#size-buttons button[data-size="s"]').click()` を実行し、`#caption-screen` の `size-s` を確認する。`m`、`l` にも同じ操作を行う。原文ONは `student.locator('#ja-toggle').check()`。`hide-ja` の解除を確認し、`#settings-close` でシートを閉じる。
- **字幕本文。** 字幕配信後に原文ON/OFFを切り替え、`.card .ja` の実際の可視性を確認する。`#settings-btn` でシートを開いて言語selectを `zh` にし、閉じてから次の字幕が中国語へ切り替わることも確認する。

## Gotchas

- 文字サイズのラベルは日本語・中国語では小/中/大、英語ではS/M/L。操作には表示文言でなく `data-size` を使う。
- 初回の自動proofは字幕が無い状態で、コントロール・CSSクラスと設定保持を確認する。本文の可読性や実翻訳の切替は未検証。
- 言語は原文・文字サイズと異なりlocalStorage保持の対象ではない。
- 不足する音声を内部字幕注入で補わない。字幕本文の操作は音声を用意してから実行する。
