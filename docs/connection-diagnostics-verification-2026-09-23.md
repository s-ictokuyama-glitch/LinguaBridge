# 設定を変えない段階診断の現地外検証（Issue #36）

2026-09-23、開発用Windows PCで実施。現地Wi-Fi・対象端末は使っていない。
本書は**実装・自動試験・案内の照合結果であり、現地での接続復旧の合格ではない**。現地での適用確認は #43 F1・F8 で行う。
自宅・ループバック・模擬条件の成功は、現地受入の証拠にしない。

仕様: https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/36

## 対象コミット

- `6dd2413` feat: add read-only staged connection diagnostics (#36)。以後 `d0e9182`（#40 証明書）・`fa9fa9b`（#38 NIC選択）で診断の証明書・IP選択部分が更新済み。
- 本書と同じコミット（#36 の残項目）。変更点は下記「今回の変更」。
- 照合時点で `6dd2413` 以降の7コミットはローカルブランチ `localServer2` のみにあり、GitHubへは未push。push後にissueへ参照を付ける。

## 実行環境

Windows 11 Home 10.0.26200、Python 3.12.10（MSC v.1943 64 bit）、Windows PowerShell 5.1.26100、
fastapi 0.139.0、uvicorn 0.50.2、cryptography 49.0.0、pytest 9.1.1、mypy 2.1.0。

## 受入条件との照合

| 受入条件（現地外） | 判定 | 根拠 |
|---|---|---|
| ネットワーク候補・採用IP・設定ポート・待受プロセス・ランタイム版・TLS/FWの確認状態を表示し、固定ポートを前提にしない | 満たす | `server/diagnostics.py` の `diagnose`／`print_report`、`scripts/diagnose_windows.ps1`（設定ポートだけの待受・FW規則を読取）。試験: `test_unavailable_observations_are_unknown_and_report_has_no_secrets`（ポート18081/18444）、`test_help_uses_configured_ports_and_separates_unconfirmed_stages`、`test_next_steps_separate_server_remote_models_and_stages`（8000/8443が出ないこと） |
| 診断前後でOS設定を変えず、権限不足・取得不能を未確認とする | 満たす | `test_windows_launcher_preserves_os_settings`（`start.bat --diagnose` と同じ `run.ps1 -Diagnose` の前後で、FWプロファイル・規則、IPv4、DNS、実行ポリシー、ルート証明書、設定・証明書ファイルが同一。本環境では権限不足によるskipなしで実行）。`test_unavailable_observations_are_unknown_…`・`test_text_report_shows_unobtainable_as_unconfirmed`（JSON・テキストとも権限不足は未確認、「確認済み」なし、例外詳細を出さない）。`test_slow_firewall_does_not_discard_listener_results` |
| サーバー自身と別端末の死活確認、モデル準備、最終URL、HTTP・TLS・ページ・WSを分けた案内。ping失敗やログ不在だけでAP分離と断定しない | 満たす | `next_steps`、`web/connection-help.html`、`docs/connection-diagnostics.md` の段階表。試験: `test_live_http_is_distinct_from_models_and_remote_checks`（`/ready` 503をモデル未準備として分離、別端末は全段階未確認）、`test_next_steps_…`、`test_certificate_failure_points_to_recovery_not_remote_trust`、`test_help_without_selected_ip_keeps_guidance_but_hides_remote_urls` |
| 通常利用者向け説明と担当者向け詳細、記録欄（時刻・端末/ブラウザ版・URL・実エラー・対応ログ・次段階）。参加コードを伏せ、秘密鍵を収集しない | 満たす | ヘルプページ冒頭（利用者向け）と `<details>`（担当者向け）。記録欄は `RECORD_FIELDS` 1か所から手順書・ヘルプページ・`--json` の `record` へ揃える。試験: `test_record_templates_share_one_field_list`、参加コード・`PRIVATE KEY` が出力に無いこと（上記試験と `test_tls_checks_ip_and_key_without_changing_or_exporting_files`） |
| 実施したローカル試験・対象コミット・残課題を記録し、#43 F1・F8への引き継ぎを揃える | 本書 | 下記 |

## 今回の変更

照合で見つかった不足を試験先行で補った。

1. 採用IPを取得できないとき、テキスト出力が `採用IP: None`、案内が `採用IP None は…` になっていた。どちらも「未確認」と表示する。
2. 記録欄の項目が3箇所（手順書・ヘルプページ・`--json`）で不揃いだった。
   - 手順書にだけ「サーバーPC／別端末のHTTP」「最後に成功した段階」があり、ヘルプページにだけ「証明書警告」「マイク・字幕」「FW修復の要否」があった。
   - 一覧を `server/diagnostics.py` の `RECORD_FIELDS` 1か所にまとめ、3箇所の和集合（18項目）にした。
   - ヘルプページはこの一覧から描画する。`--json` の `record` も同じキー順で出力し、`config_file` には実行時の `--config` が入る。
   - 手順書の記録欄は、一覧と完全一致することを試験で照合する。
   - `record` のキーは `http`／`tls`／`page`／`ws_join` から `server_http`／`remote_http`／`remote_tls`／`remote_page`／`remote_ws_join` 等に変わった。`6dd2413` は未pushで、他に参照箇所はない。
3. 接続先IPを確認できないと、`/connection-help` が例外文だけを返していた（利用者向け説明なし）。
   - 503のまま、利用者向け説明・サーバーPC内の確認（ループバックの `/healthz`、`start.bat --diagnose`）・記録欄を表示する。
   - 別端末用のURL、HTTPSのURL、それを前提にする証明書警告・再発行後の再確認の段落は出さない（#38の「古い案内を再公開しない」を維持）。
4. 診断が証明書のIP・期限・鍵の**失敗**を観測したときだけ、次の操作に再発行コマンドと `docs/certificate-recovery.md` を表示する。
   - コマンドには実行時の設定ファイルと採用IPが入る。未確認は不整合と断定しない。
   - 再発行そのものは#40の機能で、ここでは診断から既存の復旧手順へ誘導するだけ。
5. `tests/integration/test_network_advertising.py` の `inspect_tls` スタブを、実際の戻り値と同じく `certificate`・`key_pair` を含む形に直した。

## ローカル試験

- 診断関連: 次の4ファイル計48件が成功した。
  - `tests/integration/test_ops.py`（`TestConnectionDiagnostics` 10件を含む）
  - `tests/integration/test_ops_certificates.py`
  - `tests/integration/test_network_advertising.py`
  - `tests/integration/test_certificate_workflow.py`
- 型検査: `python -m mypy`（`server` パッケージ32ファイル）で問題なし。
- 全体: 下記「全体試験の結果」。
- 実機の読み取り診断: 本PCで `python -B -m server.diagnostics` と `--json` を実行し、終了コードは0だった。出力にはIP・プロセス・FW情報が含まれるため、リポジトリには含めない。
  - 観測（IPは伏字）: IPv4候補8件から1件を採用。設定ポートはHTTP 8000／HTTPS 8443。待受2件を取得。FW規則一覧は取得済みで、実効許可は未確認。
  - ループバック・採用IPのHTTP、`/ready`、ページ取得、提供中証明書のSHA256一致、HTTPS `/healthz` は成功した。
  - 証明書は「SAN不一致」で失敗、鍵の一致は成功した。本PCの証明書が別IP向けに発行されていたためで、手元の状態であり現地の証拠ではない。この観測が上記変更4のきっかけになった。
  - 別端末の4段階は未確認のまま。

実行コマンド:

```powershell
.venv\Scripts\python -m pytest tests/integration/test_ops.py tests/integration/test_ops_certificates.py tests/integration/test_network_advertising.py tests/integration/test_certificate_workflow.py -q
.venv\Scripts\python -m mypy
.venv\Scripts\python -B -m server.diagnostics --config config.yaml --json > <共有しない場所>\connection-report.json
```

### 全体試験の結果

`.venv\Scripts\python -m pytest -q`: 980件成功、5件skip、23件失敗・3件エラー。

- 失敗・エラーは次の6ファイルに限られる。いずれも本issueと別作業の、未追跡または未コミットの証拠系試験。
  - `tests/unit/test_field_story_evidence.py`
  - `tests/unit/test_perf01_evidence.py`
  - `tests/unit/test_qa_browser_runners.py`
  - `tests/unit/test_scale_bench.py`
  - `tests/unit/test_text_metrics.py`
  - `tests/unit/test_validate_field_performance.py`
- 本変更だけを退避した状態でも、同じ23件失敗・3件エラーになった。本変更による失敗ではない。
- 診断に関係する試験はすべて成功した。
- 1回目の全体実行では、7%付近でリポジトリ走査中の20秒タイムアウトが1回発生した。再実行では再現していない。

## 残課題

| 残課題 | 追跡先 |
|---|---|
| WS参加は診断で自動確認しない。参加コードのAPI・参加WSを呼ばない設計のため、別端末での手動確認（先生ページの人数照合）に委ねる | 設計上の制約（本issue）。現地確認は #43 F1・F5 |
| ランタイム版は診断を実行したPython環境の値。別環境で起動したサーバーの版は確認できない | 本issue。現地では #43 F1 で起動と同じ `.venv` から実行したことを記録する |
| FWは規則一覧までで、実効許可は常に未確認。別端末のHTTP比較で判断する | #39（修復の要否）、#43 F3 |
| `start.bat --diagnose` は `--config`・`--advertise-ip` を渡せない。カスタム設定や今回限りのIPで起動した場合は、Pythonの入口を案内している | 本issue（手順書で案内済み）。現地では #43 F1 |
| OS設定の前後比較試験は `start.bat --diagnose` と同じ `run.ps1 -Diagnose` の入口だけを通す。`--config`・`--advertise-ip` 付きのPython入口は同じ `diagnose()` を使うため読み取り処理は共通だが、その入口での前後比較は直接試験していない。Windows限定で、権限不足の環境ではskipし、そのときは未確認として扱う | 本issue |
| この開発PCの証明書はSAN不一致のまま。手元の状態であり、現地の証拠ではない | #43 F4 |

## #43への引き継ぎ

- **F1**: F1で記録する項目の出どころは次のとおり。
  - 対象コミット: `git rev-parse HEAD`
  - 設定ファイル: `record.config_file`
  - Python等の版: `runtime`
  - 実Wi-FiのNIC・IP: `network.interfaces`・`network.selected_ip`、OS側の一覧は `interfaces`
  - HTTP／HTTPSポート: `ports`
  - 待受PID: `listeners.items[].OwningProcess`
  - 端末・OS・ブラウザ版: 記録欄の「端末・OSの版」「ブラウザの版」に手書きする（診断はサーバーPCしか見ない）
- **F1（実行）**: 起動と同じ設定・選択IPで `.venv\Scripts\python -B -m server.diagnostics --config <設定> --advertise-ip <当日IP> --json` を実行する。`record.config_file` に設定ファイルが入る。
  - `network`、`ports`、`listeners`、`runtime` から、NIC・IP・ポート・待受PID・版を記録する。
  - `probes` の `loopback_http`／`selected_ip_http`（通信）と `models`（モデル準備）は分けて扱う。
  - `remote` の4段階は、別端末で確認するまで未確認のまま残す。
  - 採用IPが未確認のとき `/connection-help` は503で、別端末用URLを出さない。この状態では接続先の再選択が先になる。
- **F8**: 記録欄は手順書・ヘルプページ・`--json` で同じ18項目になっている（`RECORD_FIELDS`）。
  - 項目: 時刻、設定ファイル、端末・OSの版、ブラウザの版、入力URL、最終URL、サーバーPCのHTTP、別端末のHTTP・TLS、証明書警告・承認可否、別端末のページ取得・WS参加、マイク・字幕、FW修復の要否、実エラー、対応ログ、最後に成功した段階、次に試す段階。
  - 参加コードは手作業で伏せる。秘密鍵・証明書フォルダは添付しない。JSONにはサーバーPCのIP・プロセス・FW情報が含まれるため、担当者間で管理する。
  - 本書の実機診断は開発PCのループバックでの結果であり、現地のF1結果の代わりにしない。
