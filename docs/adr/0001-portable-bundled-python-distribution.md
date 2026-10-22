# サーバーPCへは Python 同梱のポータブル配布パッケージで届ける

サーバーPCには開発環境を入れず、導入時のネット接続も前提にしないため、埋め込み版 Python・依存パッケージ・既定モデル（＋NLLB予備）を同梱した zip を開発機でビルドし、`C:\LinguaBridge\` に展開して `start.bat` で起動する形にした。従来の `setup.ps1`（サーバーPC上で venv を作り pip とモデルDLを行う）は開発機用として残す。

## Considered Options

- **PyInstaller で exe 化**: 見た目は単一 exe だが、llama.cpp / sherpa-onnx / CTranslate2 のネイティブDLLや遅延 import の取りこぼしが起きやすく、学校のウイルス対策ソフトによる誤検知・隔離のリスクが高い。先生の操作はポータブル版でも「ダブルクリック」で変わらないため利点が薄い。
- **Docker**: Docker Desktop + WSL2 の導入が重いうえ、コンテナからはサーバーPCの Wi-Fi が見えず、稼働中NICからのIP選択・QR生成（#38）やモバイルホットスポット構成が成り立たない。

## Consequences

- まっさらな Windows 11 には Visual C++ ランタイムが無いことがあるため、`vc_redist.x64.exe` を同梱し、初回起動時のUAC昇格処理（ファイアウォール許可・スリープ無効化と同じ場面）でインストールする。
- 自己署名証明書は SAN にサーバーPCの LAN IP を含むため同梱できず、初回起動時にサーバーPC上で生成する。
- 更新は本体領域（app）の差し替えで行い、証明書・授業記録・設定の上書きは現地データ領域（data）に置いて引き継ぐ。
