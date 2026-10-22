# LinguaBridge 機能マップ

検証する経路と合格条件の正本。このマップは操作レシピを持ち、既存 `docs/qa/feature-status.xlsx` の合否台帳を置き換えない。
今回実行した範囲は [検証記録](../references/proof.md) に限定する。

## Baseline preconditions

- SKILL.md のlaunchで作った所有runに対してdoctorが成功している。
- 先生は `/teacher`、生徒は `/`。言語はen/zh。コードは先生画面から読む。
- DB、認証、既存授業のseedは不要。空データ、記録OFF、未開始から始める。
- 先生用contextは1つ。先生タブの追加は既存先生を切断する。
- 別runは別ポート、データ領域、browser contextを使う。

## Driving conventions

実インスタンスの参加・表示設定は `scripts/student_preferences.py` で駆動する。
機能ファイルの `teacher` と `student` はそのスクリプトと同じ独立context内のPlaywright Pageを表す。
拡張操作は同スクリプトを参考にrun専用のPythonスクリプトに記述し、doctor後に実行する。共通ヘルパーを一時的に書き換えない。
操作前にtraceを開始し、スクリーンショット、ARIA、assert結果をrun配下へ保存する。
実音声の経路ではEdge contextに `permissions=["microphone"]` を指定し、検証担当者が検証用の日本語を発話する。
発話できない場合は、Edgeのファイル音声入力で検証用WAVをマイクの代わりに使える。詳細はSKILL.mdのEvidenceを参照する。
その場合は物理収音を未検証とし、音源・送出PCM・実モデル・実効設定を証拠へ残す。内部setterから字幕を作らない。

以下の既存QAは、この開発機の未追跡ファイル `scripts/qa_native_browser.py` と `scripts/qa_browser.py` に依存するローカル補助試験。スキル単体には同梱しない。使用前にファイルの存在と依存を確認し、無ければ該当する補助ケースを未検証として記録する。製品CLIの直接操作は継続できる。
既存QAの補助コマンドはrepoルートで実行する。`$env:PYTHONIOENCODING='utf-8'` を設定する。
native QAは実HTTP/WS・Edge、energy VAD、fake ASR/MT、模擬マイクを使う。実モデル品質の合格条件とは区別する。
`qa_browser.py` はHTTPとWSも置換するためUIの局所検証として使う。
既存QAは秒単位のrun名なので同一秒に並列起動しない。出力された `docs/qa/runs/.../browser.json` を読み、対象が空でないこととscopeを確認する。
台帳への同期は依頼された場合だけ行う。

## Proof and skip reporting

全入口を列挙し、今回通した入口と未実施の入口を結果へ記録する。1入口の成功を機能全体の合格にしない。
音源、マイク、端末、モデル、学校LANの不足で到達できない場合は、コマンドと不足条件を保存する。
結果画面だけでなく、trace内の操作、第二画面の状態、保存ファイルを確認する。
cleanupでデータを消す前に記録ファイルを証拠側へコピーする。

## Features

- [授業への参加](join.md) は手入力、URLクエリ、言語選択、誤コードを扱う。
- [生徒の表示設定](student-preferences.md) は原文、文字サイズ、言語変更、保持を扱う。
- [先生の授業操作](lesson-control.md) は開始、一時停止、再開、終了、マイクを扱う。
- [字幕と接続復元](captions-reconnection.md) は配信、追従、切断復元を扱う。
- [授業記録](recording.md) は記録表示、記録ON/OFF、終了時の保存を扱う。
- [接続の案内](connection-help.md) は先生・生徒のヘルプ入口、設定に応じたURL、診断案内を扱う。
