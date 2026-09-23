# 証明書の検証・再発行・復元の現地外検証（Issue #40）

2026-09-23、開発用Windows PCで実施。現地Wi-Fi・対象端末は使っていない。
本書は**現地外の実装照合と自動試験・手元環境での確認の証拠**である。社内Wi-Fiでの公開TLS、
対象端末の警告承認、実運用ペアの復元の証拠ではない。現地確認は #43 F4・F7 で行う。

仕様: https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/40
照合した実装: `d0e9182`（検証・再発行・復元）。`origin/localServer2` に含まれることを確認した。
照合開始時のHEADは `f438d94`。本書と追加の修正・試験は、本書を追加したコミットに含まれる。

## 判定

| 受入条件（現地外） | 判定 | 根拠 |
|---|---|---|
| 1. 接続先確定後のSAN・有効期間・鍵の検証と、正常の根拠・不整合の内容・再発行操作・未確認の表示 | **実装・試験済み（d0e9182）** | `server/certificates.py` を起動案内・先生情報API・診断・証明書CLIが共用する。接続先未確定なら `unknown`。正常時はSAN・有効期間・検証時刻・SHA256を出力する。不整合時は理由と `make_cert.py --config … --advertise-ip … --force` を表示し、HTTPS待受を開始せずlocalhostの先生URLを案内する。ファイルの存在だけでHTTPSを開始・案内する箇所は残っていない（`config.yaml` のコメントだけが旧表現だったので修正） |
| 2. 確定した接続先での再発行と、旧証明書・鍵の一組の退避・復元（一時設定・一時証明書） | **試験済み。本日、本番待受での試験を追加** | 既存試験で、退避→置換→復元のバイト一致、2つ目のファイルの置換失敗時の自動復元、一方だけ欠けた一組、壊れた退避の拒否、接続先未確定時に変更しないことを確認済み。今回、本番の `_serve` を再起動して、再発行・復元が提供中のTLSに反映されることを確認した（下記） |
| 3. 古いSAN・期限切れ・鍵不整合・証明書なしの試験 | **試験済み（d0e9182）** | `test_bad_certificate_never_reports_remote_ready` が、期限切れ・有効期間前・鍵不整合・証明書なし・形式不正・読取権限なしを本番の起動入口で確認する。古いSANは `test_stale_san_is_not_advertised_as_remote_ready`。期限30日前の案内は `test_ops_certificates.py` |
| 4. 手元の通信可能な環境での公開TLS入口、設定ファイルと提供中証明書の一致、対象ブラウザの警告・HTTPS再確認手順 | **本日、手元環境で確認** | 下記「手元環境での確認」。手順は [certificate-recovery.md](certificate-recovery.md)「対象ブラウザでの警告承認と再確認」。Chromebook Chrome・iPhone Chromeの実際の操作ラベルは現地で確認する（F4） |
| 5. PC外への証明書導入・ポリシー変更なし。警告承認・HTTPS・マイク・字幕の別判定。FW等の未解決の引き継ぎ | **確認済み** | 診断は `remote_trust` を常に `unknown` とし、`https_health` の文言にも「別端末・マイク・字幕は未確認」と出す。手順書は証明書インストール・設定変更での回避を禁止し、FW修復の要否を記録欄に持つ。本日の作業でもOSの信頼ストア・FW・既存の `certs/` は変更していない |
| 6. ローカル試験・対象コミット・復元手順・未確認の対象端末の記録と、#43 F4・F7への引き継ぎ | **本書で記録。GitHub上の引き継ぎは未掲載** | 本書末尾。本書をコミットした時点では、#40・#43への掲載・相互リンクは行っていない |

## 照合で見つけて直した退行

`d0e9182` で `make_cert.py` の既存ファイルありの動作が変わった。変更前は、既存の一組があれば常に終了コード0だった。
変更後は、既存の一組が採用IPに対して不整合、または接続先が曖昧だと終了コード1で終わるようになった。
`setup.ps1` はこれを失敗として中断し、完了マーカーを書かない。すると `start.bat` が毎回セットアップを再実行して中断し、**サーバーを起動できなくなる**。
一方、起動時は同じ検証で不整合を検出し、このPC用のHTTP先生URLで起動を続けられる。

修正内容:

- `make_cert.py`
  - 既存ファイルを変更せず、正常とも確認できない場合は終了コード `3`（`EXIT_UNCHANGED_NOT_READY`）を返す。argparse の引数エラー（2）と区別するため3にした。一組の片方だけの場合も含む（起動時の検証で縮退する）。
  - 既存ファイルがあり自動選択の接続先が未確定のときも、生成失敗（1）ではなく未確認として `3` を返す。明示した `--advertise-ip` がこのPCにない場合は入力の誤りとして `1`。
  - 生成・再発行・復元の失敗は従来どおり `1` を返す。
- `setup.ps1`: `3` の場合は検証結果の確認と復旧手順を警告として表示し、セットアップを続ける。

証明書の置換を伴う操作（`--force`・`--restore`）の動作は変えていない。

## 今回追加・変更した試験

`tests/integration/test_certificate_workflow.py`

- `test_reissue_and_restore_take_effect_on_restarted_production_listener`（追加）
  - 本番の `server.main._serve` を、ループバック・空きポートで起動する。起動のたびにアプリを作り直し、停止→再起動を模擬する。Uvicorn・TLS・待受は実物で、推論だけフェイクにした。
  - 旧SAN（`192.168.1.35` のみ）の一組では、HTTPの待受だけでHTTPSを開始しないことを確認する。
  - `replace_pair` で再発行して再起動すると、次がすべて `ok` になり、`remote_trust` は `unknown` のままであることを確認する。
    - 証明書・鍵の検証
    - 提供中証明書のSHA256とファイルの一致
    - HTTPS `/healthz`
  - 退避した一組を復元すると、元のバイトに戻る。再起動すると、再びHTTPSを開始しない（復元成功をTLS正常と扱わない）ことを確認する。
  - 診断のTLS接続はネットワークガードの下で行い、外部接続が0件であることを確認した。
- `test_existing_pair_with_unresolved_network_is_unchanged_not_failed`（追加）: `setup.ps1` と同じく `--advertise-ip` なしで接続先が未確定でも、既存の一組を変更せず、退避も作らず、終了コード3を返すことを確認する。
- `test_explicit_ip_not_on_this_pc_fails_even_with_existing_pair`（追加）: 明示したIPがこのPCにない場合は1。
- `test_unchanged_code_differs_from_argparse_usage_error`（追加）: 終了コードが0・1・argparseの2と重ならないこと。
- `test_existing_bad_certificate_is_reported_without_overwriting`・`test_partial_pair_requires_force_and_restores_missing_file_state`（期待値変更）: 既存ファイルを変更しない場合の終了コードを1から3に変えた。

## 試験が不具合を検出できることの確認

本番コードを1か所ずつ一時的に壊し、追加した本番待受の試験を実行した。各回の後に元へ戻した。

| 破壊 | 結果 |
|---|---|
| `server/main.py` の `_serve` で、HTTPS開始の判定を `tls_ready()` から `cert_path().exists()` に変更（ファイルの存在だけで開始） | 失敗 |
| `scripts/make_cert.py` の `replace_pair` で、置換を `_write_pair(paths, original)` に変更（再発行が反映されない） | 失敗 |
| `server/main.py` の `ssl_certfile` を別のパスに変更（設定と異なる証明書を提供） | 失敗 |

## 実行結果

```
.venv\Scripts\python -m pytest tests/integration/test_certificate_workflow.py tests/integration/test_network_advertising.py tests/integration/test_ops_certificates.py tests/integration/test_ops.py -q
```

55件成功。`python -m mypy`（`server` パッケージ）と `mypy scripts/make_cert.py` はエラー0件。
`setup.ps1` はPowerShellの構文解析でエラー0件、UTF-8 BOMを維持している。

全体試験: 下記「全体試験」。

## 全体試験

```
.venv\Scripts\python -m pytest -q --ignore=tests/integration/test_real_mt.py --ignore=tests/integration/test_real_asr.py
```

コードレビュー反映後の最終状態で994件成功・5件スキップ（167秒）。
実モデルを読み込む `test_real_mt.py`・`test_real_asr.py` は、#38の記録と同じ理由で除外した（本機ではモデル読込が試験の制限時間を超える。本変更と無関係）。
本作業の対象外の未コミット変更が作業ツリーにある状態で実行した。環境は Python 3.12.10、Windows 11 Home 10.0.26200。

## 手元環境での確認

2026-09-23 22:45〜22:48 JST、自宅Wi-Fi。NICの自動選択は `192.168.1.6` だった。
一時設定・一時証明書ディレクトリ・一時ポート（HTTP 18600／HTTPS 18643）を使い、音声・翻訳はフェイクにした。
既存の `certs/`・`config.yaml`・OSの信頼ストア・FWは変更していない。

1. 旧SAN（`192.168.1.35`・`127.0.0.1`）の一組を置き、`make_cert.py --config <一時設定>` を実行した。
   - `SAN不一致（採用IP 192.168.1.6）` を表示し、ファイルは変更せず終了コード2で終わった（このときの値。コードレビュー後に3へ変更した）。
   - 旧SHA256: `505ccf89…4ac7390`
2. `--force` で再発行した。
   - 旧一組は `backups/20260923T134551Z-8575117c/` に退避された。
   - 新SANは `127.0.0.1`・`192.168.1.6`、検証 `ok`、終了コード0。
   - 新SHA256: `f3a1757b…fa41a3c3`
3. `python -m server.main --config <一時設定>` で起動すると、`0.0.0.0:18600` と `0.0.0.0:18643` の両方で待ち受けた。
   `python -m server.diagnostics --config <一時設定> --advertise-ip 192.168.1.6 --json` の結果:
   - `certificate` ok、`key_pair` ok
   - `served_certificate` ok（提供中SHA256 = ファイルのSHA256 `f3a1757b…`）
   - `https_health` ok（HTTPS 200・`status:ok`）
   - `remote_trust` unknown

   一時証明書を当該検証プロセスだけで信頼し、SAN照合を有効にした接続も行った。
   公開IPのHTTPS `/healthz`・`/teacher` は200で、先生情報APIの先生URLは `https://192.168.1.6:18643/teacher` だった。
4. 待受プロセスのコマンドラインが一時設定のものであることを照合して停止し、待受0件を確認した。
   - `--restore <退避先>` で復元すると、復元前の一組は別の退避先へ保存された。
   - 証明書・鍵ともに退避元とSHA256が一致した。
   - 復元後の検証は `SAN不一致` になり、再確認の終了コードは2だった（同上）。旧状態が正しく戻ったことを示し、TLS正常ではない。
5. 終了コードを3へ変更した後、同じ一時設定で再実行した。
   - 復元済みの旧SANの一組: `SAN不一致` で終了コード3。
   - `--advertise-ip 10.99.99.99`（このPCにないIP）: NIC一覧と「証明書を生成できません」を表示して終了コード1。ファイルは変更しない。

2026-09-16の手元確認と2026-09-17の現地サーバーPC側の再発行（[9月17日の記録](connection-field-results-2026-09-17.md)）に続く確認である。
いずれも別端末の警告承認・マイク・字幕の証拠ではない。

## 変更していないもの

- 既存の `certs/`、OSの信頼ストア、FW、ネットワーク設定。試験・確認は一時ディレクトリだけを使った。
- 別端末・社内ネットワークへの操作。

## #43への引き継ぎ

- **F4**
  1. 当日の実Wi-Fi IPを確定したら、`start.bat` の起動画面の検証結果を控える。
     - TLS証明書・鍵の状態、SAN IP、有効期間、SHA256
  2. `start.bat --diagnose` または `python -m server.diagnostics --advertise-ip <IP> --json` で次を照合する。
     - `served_certificate`・`https_health`
  3. 9月17日に再発行した一組（SAN `10.53.64.130`、SHA256 `22329046…abc4f454`）は、当日のIPが同じなら再発行不要の見込み。
     IPが変わっていれば `SAN不一致` と再発行コマンドが表示されるので、[certificate-recovery.md](certificate-recovery.md) の手順で停止・再発行・再起動・再診断する。
  4. Chromebook Chrome・iPhone Chromeで `/healthz` と `/teacher` を開き、手順書の記録欄に沿って次を分けて記録する。Braveは#37の合意済み省略として扱う。
     - 最終URL
     - 警告文言・コード
     - 進む操作のラベルと可否
     - ページ表示・WSの結果

  ローカルでは対象端末の警告画面・操作ラベルを確認していない。
- **F7**: 実運用ペアの復元は、まず現在の正常な一組とSHA256を控える。次に、サーバー停止中に `--restore` を使う。
  - ローカルでは次を確認済み。
    - 復元前の一組を別に退避すること
    - バイト一致で戻ること
    - 旧SANを戻すとHTTPSを開始しないこと
  - 確認後は当日のIPに合う一組へ再度 `--restore` で戻し、再起動して公開TLSと別端末の到達を再確認する。
  - 9月17日の退避先 `certs/backups/20260917T021006Z-0bc81ee9/` は旧SAN（`192.168.1.35`）の一組である。復元すると不整合になるのが正しい結果。
- **setup.ps1の変更**: 現地PCで再セットアップが走っても、既存の証明書が不整合なら警告して続行する（終了コード3）。現地で `setup.ps1` を実行した場合は、警告の有無を記録する。
- **残課題**
  1. 対象端末での警告表示・承認操作・HTTPS到達（F4）。
  2. 社内サーバーPCでの実運用ペアの復元と復帰後の別端末再確認（F7）。
  3. サーバーPCだけが成功する場合のFW修復の要否（#39・F3）。

  いずれも現地確認待ち。マイク・字幕は#41（F5）で扱う。
