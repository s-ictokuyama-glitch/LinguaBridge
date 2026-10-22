# 生成したスキルの検証記録

この記録の `artifacts/`、任意設定、補助QA・回帰テストは、この開発機にのみ存在する未追跡ファイルを含む。スキル単体のPRには同梱せず、別のcheckoutでは証拠リンクや再実行スクリプトを利用できない。ファイルが無い場合は本記録を実行証拠として使わず、SKILL.mdのlaunchから新しい証拠を作る。

初回の参加・表示設定試験を以下に記録する。
その後の音声配信・記録保存は [追加検証結果](audio-recording-proof.md) を参照する。
「今日は植物」の誤認識調査と任意プロファイルでの再検証は [改善の検証記録](asr-opening-fix-proof.md) を参照する。

2026年10月21日、Windowsのこのcheckoutで実施した。
`.venv` Python 3.12.10、Edge 155.0.4283.45、既存ReazonSpeech K2 v2とHy-MT2を使用した。

## 完走した試行

実行ディレクトリは [20261021-skill-proof-04](../../../../artifacts/verify/20261021-skill-proof-04/)。
次の起動コマンドからdoctor、drive、cleanupまで実行した。

```powershell
.venv/Scripts/python.exe .agents/skills/verify/scripts/instance.py launch --run-dir artifacts/verify/20261021-skill-proof-04 --models-dir C:\Users\esesm\AppData\Local\LinguaBridge\models --advertise-ip 192.168.1.6
.venv/Scripts/python.exe .agents/skills/verify/scripts/instance.py doctor --run-dir artifacts/verify/20261021-skill-proof-04
.venv/Scripts/python.exe .agents/skills/verify/scripts/student_preferences.py --run-dir artifacts/verify/20261021-skill-proof-04
.venv/Scripts/python.exe .agents/skills/verify/scripts/instance.py cleanup --run-dir artifacts/verify/20261021-skill-proof-04
```

- [doctor.json](../../../../artifacts/verify/20261021-skill-proof-04/doctor.json) でPID、ポート所有、ソース・設定一致、モデルreadinessと参加コードを確認した。
- [server.stderr.log](../../../../artifacts/verify/20261021-skill-proof-04/server.stderr.log) に実sherpa ASRとHy-MT2のロード・ウォームアップ完了を記録した。
- [result.json](../../../../artifacts/verify/20261021-skill-proof-04/student-preferences/result.json) はpass。手入力参加、L設定、原文OFF、reloadと再参加、設定保持、zh変更、クエリ経由参加を確認した。先生の生徒数1、言語内訳 `zh: 1`、ページエラー0件、記録ファイル0件も照合した。
- [先生画面](../../../../artifacts/verify/20261021-skill-proof-04/student-preferences/04-teacher.png)、[生徒画面](../../../../artifacts/verify/20261021-skill-proof-04/student-preferences/04-student.png)、各ARIA、`0-trace.zip` と `1-trace.zip` に操作と結果を保存した。
- [ownership-guard.json](../../../../artifacts/verify/20261021-skill-proof-04/ownership-guard.json) で、生成時刻が違うPID情報ではcleanupが終了コード1で拒否し、本来のサーバーが生存することを確認した。
- [post-cleanup.json](../../../../artifacts/verify/20261021-skill-proof-04/post-cleanup.json) で実サーバー停止、専用tmp削除、cleanup再実行の成功、証拠ファイル残存を確認した。

## 起動時に解消した条件

`skill-proof-01` はNIC種別取得不可による自動IP選択失敗。実Wi-FiのIPv4を読み取って明示した。
`skill-proof-02` はサンドボックス内のWindows TCP所有者取得失敗。通常ユーザー権限で再実行した。
`skill-proof-03` は `.venv` ランチャーと実サーバーのPIDの差を検出した。ヘルパーを修正し、起動した子プロセスのうち対象ポートを所有するプロセスを記録した。
全試行にcleanupを実行し、失敗ログは各runへ残した。

この実行環境ではサンドボックスの `%LOCALAPPDATA%` が通常のモデル格納先と異なる。
既存モデルの実パスを明示した。モデルの再ダウンロード、証明書発行、OS設定変更は行っていない。

## 未検証の経路

製品CLI、実サーバーと実Edgeを使用した。コンテナや代替実装は使っていない。
授業音声の収音、ASRから訳文までの配信、字幕本文の原文表示、記録保存、学校LAN、別端末HTTPS、物理QR読取りは今回駆動していない。
実モデルはロードとウォームアップまで確認した。認識・翻訳の意味精度の証明ではない。
配布先のWindowsでは `start.bat`、UAC、ランタイム導入、ファイアウォール、スリープ設定、証明書、モデル配置と実NIC選択を別途実施・検証する。

## 構造検証

両Pythonヘルパーを構文解析し、SKILLのfrontmatterを `skill-creator/scripts/quick_validate.py` で検証した。
日本語UTF-8のためWindowsではPythonに `-X utf8` を付ける。
機能ファイルの四つのH2、Markdownリンクの実在を照合した。
独立した読み取り専用レビューで、実行手順・所有確認・cleanup・証拠・検証範囲に高優先度ブロッカーは指摘されなかった。
