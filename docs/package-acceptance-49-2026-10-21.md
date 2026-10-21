# Python の無い別PCでの配布パッケージ受け入れ確認（#49）

仕様: [Issue #49](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/49)（親 #44）。
[Issue #16](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/16) の受け入れ条件1
（まっさらな Windows 11 機で配布パッケージの展開＋起動バッチだけで運用可能になる）の判断材料。

- 実施日: 2026-10-21
- 配布パッケージ: `LinguaBridge-20261019-1940-6673392-dirty-001.zip`（commit `6673392bf6f0c8420c875463f40f0a4dcbe7d342`、dirty: True、ビルド: 2026-10-19T19:40:23+09:00）
- 受け渡し: クラウドの共有リンクからブラウザでダウンロード
- 別PC: Windows 11 26H2（ビルド 26300.9711）
- 総合: **達成**（初回は 3b が失敗。#52 の修正後のパッケージで 3b を再確認して成功。下の「再確認」）

判定は 成功／失敗／未確認／根拠付き非該当／合意により省略 の5つ。参加コードは記録しない。
Python・Windows の版・ブロックの印・展開先・完了マーカー・first-run.log は記録用の bat が自動で調べた。

## 結果

| # | 受け入れ条件 | 判定 | 記録 |
|---|---|---|---|
| 1 | 別PCで `where python` が該当なし | 成功 | where python: C:\Users\esesm\AppData\Local\Microsoft\WindowsApps\python.exe（動かないスタブ） ／ where py: 見つからない |
| 2a | 展開前のブロックの解除 | 成功 | 「ブロックの解除」で印（MOTW）が外れたことを確認 |
| 2b | `C:\LinguaBridge\` への展開 | 成功 | 直下に start.bat と app\ ができた |
| 2c | 初回の `start.bat` と UAC の結果 | 成功 | UAC が1回出て「はい」→ 緑の完了表示 → サーバーが起動した ／ SmartScreen: 出なかった ／ OneDrive の注意: 出なかった ／ 完了マーカー: あり ／ first-run.log の [エラー]: なし |
| 3a | 先生ページが HTTPS で開く | 成功 | HTTPS で開き、警告を承認してマイクを許可できた |
| 3b | 別端末の生徒ページに字幕が届く | 成功（再確認） | 再確認で翻訳された字幕が届いた（下の「再確認」）。初回は失敗: 参加できたが字幕が出なかった ／ iPhone Chrome・英語 ／ 遅れ: 先生ページには日本語の字幕が表示されたが、生徒ページには文字起こしの字幕が表示されませんでした。翻訳の字幕は先生ページ及び生徒ページ両方届かなかった。 |
| 4 | 2回目の起動で UAC が出ない | 成功 | UAC も SmartScreen も出ずに起動した |

## #48 に戻す点（手順書の修正）

モデルの事前ロードに失敗した

（追記: 調べた結果、これは手順書の問題ではなく配布パッケージの不具合だった。下の「3b の原因」を参照。
手順書どおりの操作で詰まった箇所はなかった）

## 3b の原因（開発機での調査）

起動画面（スクリーンショット4枚）では、音声認識（reazonspeech-k2-v2）のロードとウォームアップは完了した。
続く Hy-MT2 の `Llama(...)` → `llama_model_load_from_file` で次の例外になり、事前ロードに失敗している。

    OSError: [WinError -1073741795] Windows Error 0xc000001d

- `0xc000001d` は STATUS_ILLEGAL_INSTRUCTION（その CPU が実行できない命令）。
- 同梱した `llama-cpp-python 0.3.33`（`requirements.txt` の CPU 版インデックスから取得したビルド済み wheel）は、
  `llama_print_system_info()` が `AVX512 = 1` を返す。つまり AVX-512 を前提にビルドされている。
  `ggml-cpu.dll` は1つだけで、CPU に合わせて命令を選び分ける作りではない。
- 開発機の Ryzen 7 8840U（Zen 4）は AVX-512 に対応しているので動き、別PCの CPU は対応していないので落ちた、と考えられる。
  zip を作る前の自己検証は、偽のエンジンでの `/ready` と開発機での import だけなので、これを検出できない。
- 生徒ページに日本語も出なかったのは同じ原因による。翻訳に失敗した発話は字幕ごと捨てられ（`server/pipeline.py` の
  `MT failed; ... ` の分岐）、生徒への字幕は翻訳文と日本語を1枚にまとめて送るため（`web/student.js`）。
  先生ページの日本語は音声認識の結果なので表示された。

対処の候補:

1. その場の回避: `data\config.yaml` に `mt: { engine: nllb }` を書く（README の NLLB への切替）。CTranslate2 は
   CPU に合わせて命令を選ぶので、別PCでも動く見込み。NLLB は非商用ライセンス。
2. 本対応: AVX-512 を前提にしない llama.cpp を同梱する（CPU を起動時に判定して命令を選ぶビルド
   `GGML_BACKEND_DL` + `GGML_CPU_ALL_VARIANTS`、または AVX2 までに抑えたビルド）。あわせて `build_package.py` の
   自己検証で、同梱する llama.cpp が AVX-512 前提ならビルドを止める。

## 再確認（#52 の修正後、同日）

- 配布パッケージ: `LinguaBridge-20261021-1849-6673392-dirty.zip`（#52 の修正を含む。コミットは
  [1f9607c](https://github.com/s-ictokuyama-glitch/LinguaBridge/commit/1f9607c) と同じ内容）
- 同じ別PCで、展開 → `start.bat` → 初回の UAC → 証明書の生成 → サーバー起動まで進んだ
- 起動画面: 音声認識に続き Hy-MT2 のロードとウォームアップが完了した。llama.cpp は
  `ggml-cpu-alderlake.dll`（AVX2・AVX-VNNI 版）を選んだ。この CPU に AVX-512 が無く、CPU 判定版が合う版を選んだことを示す
- 3b: **成功**。別端末の生徒ページに、翻訳された字幕が届いた（端末・言語・遅れは未記録）
- 付記: 配信中、起動画面に `ConnectionResetError(10054, '既存の接続はリモート ホストに強制的に切断されました。'…)` の
  警告（`切断済みの接続の後始末で shutdown が失敗したため、閉じて継続`）が数十行出た。字幕の配信には影響しなかった。
  ブラウザが接続を RST で閉じるたびに出る平常の記録なので、
  [752fbba](https://github.com/s-ictokuyama-glitch/LinguaBridge/commit/752fbba) で DEBUG に下げ、起動画面には出ないようにした

## #16 受け入れ条件1への所見

- 上の 1〜4 がすべて「成功」なら、開発環境の無い Windows 11 機で、配布パッケージの展開と `start.bat` だけで
  運用可能な状態になったと判断する材料になる。
- 「成功」でない項目は、原因と対処（手順書の修正なら #48、コードの修正なら新しい issue）を決めてから再確認する。