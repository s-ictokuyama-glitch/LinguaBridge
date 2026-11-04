# 配布ZIPの検証記録

2026年11月4日、Windowsの開発機で実施した。既存verifyにZIPの新規展開経路を追加し、同梱Python・依存・実モデルから先生・生徒画面を操作した。
実行領域は [20261104-distribution-zip-01](../../../../artifacts/verify/20261104-distribution-zip-01/)。本記録のartifactsと音声入力は未追跡の開発機上の証拠であり、別checkoutではSKILL.mdから新しいrunを作る。

## 対象

- ZIPは `LiveTranslation-20261103-2142-9d2c6ac.zip`、1,520,425,624 bytes。
- SHA-256は `ed5dba31854cee0394fe770924a8f6d3a7691b6923b45aea7ca2b30475beac68`。
- コミットは `9d2c6ac8677a9ec5081ea89c46e1fc56bf8aadf9`、dirty=false、Python 3.12.10。
- ASRはReazonSpeech K2 v2、翻訳はHy-MT2、VADはSilero。配布の設定を使った。
- Edgeは155.0.4283.45。案内IPは実Wi-Fiの192.168.1.6、ブラウザー接続はloopback HTTP。

検証開始時HEADのd94995bと配布元の間で、`server`、`web`、`config.yaml`、`scripts/run.ps1`、`scripts/launcher.ps1`、`start.bat` に差分がないことを `git diff --quiet 9d2c6ac HEAD -- ...` の終了コード0で確認した。
この作業では製品コードとZIPを変更していない。

## 実行と結果

リポジトリルートから次を実行した。WindowsのTCP所有確認とEdge起動には通常ユーザー環境を使った。

```powershell
.venv/Scripts/python.exe .agents/skills/verify/scripts/instance.py launch --run-dir artifacts/verify/20261104-distribution-zip-01 --distribution-zip artifacts/school-access-148-151/dist-final/LiveTranslation-20261103-2142-9d2c6ac.zip --expected-sha256 ed5dba31854cee0394fe770924a8f6d3a7691b6923b45aea7ca2b30475beac68 --expected-commit 9d2c6ac8677a9ec5081ea89c46e1fc56bf8aadf9 --advertise-ip 192.168.1.6
.venv/Scripts/python.exe .agents/skills/verify/scripts/instance.py doctor --run-dir artifacts/verify/20261104-distribution-zip-01
.venv/Scripts/python.exe .agents/skills/verify/scripts/student_preferences.py --run-dir artifacts/verify/20261104-distribution-zip-01
.venv/Scripts/python.exe artifacts/verify/20261104-distribution-zip-01/drive_audio_recording.py
.venv/Scripts/python.exe .agents/skills/verify/scripts/instance.py cleanup --run-dir artifacts/verify/20261104-distribution-zip-01
```

起動、doctor、参加・表示設定、音声・記録、最終cleanupはいずれも終了コード0。

- [package-archive.json](../../../../artifacts/verify/20261104-distribution-zip-01/package-archive.json) と [package-version.json](../../../../artifacts/verify/20261104-distribution-zip-01/package-version.json) でZIPのハッシュ、cleanな版、期待コミットを照合した。
- [package-files.json](../../../../artifacts/verify/20261104-distribution-zip-01/package-files.json) は11,657ファイルのハッシュを保持する。doctorで入力ZIPと展開全体を再照合した。
- [package-runtime.json](../../../../artifacts/verify/20261104-distribution-zip-01/package-runtime.json) はPython実体、sys.path、import元、モデル・hotwords・VAD入力を展開app内として記録した。開発モデルやcheckoutのserverを使わなかった。
- [student-preferences/result.json](../../../../artifacts/verify/20261104-distribution-zip-01/student-preferences/result.json) はpass。手入力とクエリ経由の参加、英語から中国語への切替、文字サイズL、原文OFF、再読込後の保持、先生画面の人数・言語内訳を実Edgeで確認した。ページエラー0件。
- [audio-recording/result.json](../../../../artifacts/verify/20261104-distribution-zip-01/audio-recording/result.json) は配信・保存のassertがpass。検証用日本語WAV3件をEdgeのgetUserMediaへ与え、実AudioWorklet、WebSocket、VAD、ASR、翻訳を通した。en/zh各画面のseqは1、2、3で、終了状態まで確認した。
- 記録は既定OFF、seq 1後にON、seq 2後にOFF。終了後のJSONLはseq 2だけで、原文「教科書の四十二ページを開いてください」とen/zhの字幕が保存内容・Markdownに一致した。生徒IPや識別子は保存キーに含まれなかった。
- [recording-evidence](../../../../artifacts/verify/20261104-distribution-zip-01/recording-evidence/) にJSONLとja/en/zh Markdownを退避した。[post-cleanup.json](../../../../artifacts/verify/20261104-distribution-zip-01/post-cleanup.json) で停止、tmp削除、4記録ファイルのハッシュ一致、5個の正常なtrace ZIPと20枚の画像の残存を確認した。

音声driverは既存の検証記録からrunへコピーし、SKILL.mdと機能マップの音声・記録レシピを実行した。run内に実行したコードと入力WAVのハッシュを残した。配信・保存のpassを認識・翻訳の全面的な品質合格とは扱わない。

## 失敗経路と修正

[境界検証](../../../../artifacts/verify/20261104-distribution-boundaries-02/result.json) で、期待SHA不一致、コミット不一致、展開先外パス、大小文字衝突、Windows予約名を拒否した。全5ケースでcleanupが0となりtmpを削除した。
検証用runnerの最初の試行はWindowsの出力文字コードで失敗したため、UTF-8を指定して再実行した。最初のrunにもcleanupを実行した。

[guards.json](../../../../artifacts/verify/20261104-distribution-zip-01/guards.json) で、生成時刻を変えた所有情報ではdoctorとcleanupが拒否し、本来のサーバーが生存することを確認した。展開済みCSSを変えたdoctorもinventory不一致で拒否し、復元後にdoctorが成功した。

最初の本cleanupは、Windowsのconsole子プロセスを未知の子として拒否した。[cleanup-before-fix.json](../../../../artifacts/verify/20261104-distribution-zip-01/cleanup-before-fix.json) に失敗理由を保存した。
停止対象を所有PIDと同じCLI引数のPython子へ限定した後、同じサーバーのcleanupが成功した。console子も自然終了したことを観測した。プロセス名によるkillは使っていない。

既存の開発版起動も [development regression](../../../../artifacts/verify/20261104-distribution-dev-regression/) で実行した。外部モデル指定のlaunch、doctor、cleanupがすべて0で終了し、サーバーと.venvランチャーの両方を停止してtmpを削除した。
全Pythonヘルパーの構文、SKILLのfrontmatter、機能ページの四つのH2、Markdownリンクを検証した。

## 検証範囲

開発機にはVC++ランタイム、検証制御用の.venv・psutil・Playwright・Edgeがあった。サーバーは配布のPythonと依存を使った。
空のデータ領域、専用ポート、明示した実NIC、空の証明書領域を用意し、`start.bat` の代わりに製品CLIを直接起動した。
物理マイクはWAV入力で代替した。これらは、初回導入用Windowsで未検証の準備・経路として残る。

初回UAC、VC++導入、証明書発行、ファイアウォールと電源設定、MOTW解除、NIC対話選択、2回目のstart.bat、更新後のdata引継ぎ、別端末HTTPS、学校LAN、物理収音、45分授業、ネット切断下での導入は今回未検証。
これらの入口は [配布パッケージ](../features/distribution-package.md) から別途検証する。

seq 1は「今日は植物」が「共和植物」と認識され、英語字幕に日本語が残った。既知の [Issue #89](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/89) を再確認した。品質の未解決事項として保持する。
