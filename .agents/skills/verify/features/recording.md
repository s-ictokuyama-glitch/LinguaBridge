# 授業記録

先生が記録をONにした間の発話を保存する。先生・生徒双方に記録中を表示し、終了時にローカルのJSONLと言語別Markdownへ書き出す。

## Sub-features

- `record-default-off` は既定で保存しない。
- `record-indicator` はON/OFFと途中参加者への表示を扱う。
- `record-files` は終了時の原文・訳文保存を確認する。

## How to get to it (user POV)

先生が `/teacher` の記録チェックボックスをONにして配信し、終了を押す。保存先は起動設定の `recording.out_dir`。

## Driving it with Playwright

Preconditions:

- 専用runの `instance.json.data_dir` を把握している。既存 `sessions/` を使わない。
- 実保存にはON中に1件以上の確定発話が必要。訳文保存には該当言語の生徒が必要。

- **既定OFF。** `expect(teacher.locator('#record-toggle')).not_to_be_checked()` と両画面 `#record-indicator` のhiddenを確認する。記録OFFの授業を終了し、専用data以下に `transcript*` が無いことを確認する。
- **ONと途中参加。** `teacher.locator('#record-toggle').check()`。先生と生徒、後からzhで参加した生徒の `#record-indicator` が表示される。
- **保存。** ONの間に検証用の日本語を発話する。先生原文とen/zh字幕が届いてから `teacher.locator('#end-btn').click()`。専用dataの `sessions/<日時>/transcript.jsonl` と `.ja.md`、`.en.md`、`.zh.md` を読む。seq、原文、訳文が画面と一致し、生徒のIPや識別子を含まないことを照合する。
- **OFF切替。** 別runで `teacher.locator('#record-toggle').uncheck()` 後に発話し、終了時のファイルにOFF中の発話が増えていないことを確認する。
- **補助ケース。** `.venv/Scripts/python.exe scripts/qa_native_browser.py --scenario native_recording_all_screens` はON/OFFの表示だけを確認する。保存の証明には使わない。

## Gotchas

- 全設定言語が常に翻訳されるわけではない。誰も選択しなかった言語のMarkdownは作られない。
- 0発話では保存ファイルが無い。音声が無い場合は `record-files` の前提不足として報告する。
- 記録ファイルは `tmp/data` 以下なので、cleanup前に `artifacts/verify/<run>/recording-evidence/` へコピーする。
- 既存native QAの通常記録ケースはrepo既定の `sessions/` を参照する。記録内容の試験は上記の専用製品CLIで行う。
