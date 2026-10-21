# 授業への参加

生徒は先生画面の4桁コードを入力するか、参加URLを開き、言語を選んで授業へ参加する。

## Sub-features

- `join-manual` はコード手入力で参加する。
- `join-query` は `/?code=...` のコードを自動入力する。
- `join-language` はen/zhを選び、選択した言語で開始待ちを表示する。
- `join-invalid` は誤コードを拒否し、正しいコードで再試行できる。
- `join-qr` は先生画面のQRを読み、参加URLへ移る。

## How to get to it (user POV)

先生が `/teacher` の参加コードを案内する。生徒は `/` へ手入力で入るか、先生画面のURLまたはQRから入る。

## Driving it with Playwright

Preconditions:

- 空runのdoctorが成功し、先生画面の `#start-btn` がenabledになっている。

- **手入力とクエリ。** `.venv/Scripts/python.exe .agents/skills/verify/scripts/student_preferences.py --run-dir "$verifyRun"` を実行する。`result.json` の `manual_code_join` と `query_code_join` が揃い、先生の接続数が1になる。
- **誤コード。** 別の生徒contextで `#code-input` に先生のコードと異なる4桁をfillし、`#lang-options button[data-code="en"]`、`#join-btn` をclickする。`#join-error` は `Wrong join code`、字幕画面はhidden。正しいコードで再試行し、先生の接続数増加まで確認する。
- **QR。** 補助ケースは `.venv/Scripts/python.exe scripts/qa_native_browser.py --scenario native_lan_teacher_url_qr_and_caption`。実NICの自己アドレスへの参加URL、QRのOpenCV復号、字幕までを確認する。物理スマートフォンで先生画面のQRから入る入口は別途実行する。

## Gotchas

- 同一IPで5回連続失敗すると60秒拒否される。誤コード確認は1回に留める。
- 生徒画面の再読み込みは参加画面へ戻る。表示設定の保持と自動再参加は別の挙動。
- loopbackのクエリ経由参加は学校LAN到達性やカメラのQR読取りを証明しない。
- 生徒数の統計は2秒間隔。固定sleepではなく `expect` で値を待つ。
