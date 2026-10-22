# 接続先選択の現地外検証（Issue #38）

2026-09-23、開発用Windows PCで実施。現地Wi-Fi・対象端末は使っていない。
本書は**現地外の実装照合と自動試験の証拠**であり、社内Wi-Fiでの到達・QR実読み取り・復元後の到達の証拠ではない。
現地確認は #43 F2・F7 で行う。

仕様: https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/38
照合した実装: `fa9fa9b`（接続先選択）。照合開始時のHEADは `22db0b6`。
本書と追加試験は、本書を追加したコミットに含まれる。

## 判定

| 受入条件（現地外） | 判定 | 根拠 |
|---|---|---|
| 1. #37の9月15日報告を採用根拠として記録 | **記録済み** | [9月15日の現地結果](connection-field-results-2026-09-15.md)で、実Wi-Fi `10.53.64.130` に対し、VMnet8 の `192.168.74.1` が自動案内されていた。実Wi-Fi IPの手入力で両Chromeが到達した。[network-selection.md](network-selection.md)「採用の根拠」に記録済み。本日の現地外作業では、新たな現地条件は観測していない。社内PCのNIC役割判定は未観測として残す（末尾の残課題1） |
| 2. NICの状態・役割による評価、複数候補の明示選択、移動・指定IP消失 | **実装・試験済み（fa9fa9b）** | `server/network.py`。自動採用するのは、稼働中で有効なIPv4を持つ物理NICが1つだけの場合に限る。仮想・役割不明・停止中・リンクローカル等は自動採用しない。指定IPが一意に存在しなければ例外になり、黙って置き換えない。案内後にNIC/IPが変わると先生情報APIは503を返し、同じIPが戻っても再起動まで案内を再開しない |
| 3. 起動画面・先生情報・生徒URL・QR・証明書生成の一致 | **本日、一連の流れの試験を追加** | 既存試験は各経路を個別に確認していた。今回、番号選択を起点に、1本の試験で次を通した（下記）。起動画面→API→先生画面のQR表示処理→起動画面が案内した証明書コマンド→SAN→再起動後の起動画面・APIのHTTPS先生URL |
| 4. 社内10系＋仮想192.168系、複数物理NIC、IP変更の模擬、外部接続なし | **試験済み** | 下記の試験一覧。追加した3件はすべてソケットガード下で実行し、ループバック・LAN以外への接続が0件であることを確認した。一連の流れの試験では、外部の名前解決も0件であることを確認した |
| 5. 変更前の選択設定へ戻す手順とローカル復元試験、#43への引き継ぎ | **本日、復元試験を追加。GitHub上の引き継ぎは未掲載** | 手順は [network-selection.md](network-selection.md)「元へ戻す」。今回、控え→変更→戻すの流れと、戻した値が既に無いIPの場合を試験した。引き継ぎ内容は本書末尾にある。本書をコミットした時点では、#38・#43への掲載・相互リンクは行っていない |

本番コードの変更はない。照合で不足していたのは試験と記録だけだった。

## 今回追加した試験

`tests/integration/test_network_advertising.py` に追加した。本番の起動入口 `server.main.main()`、設定ファイル、証明書CLI `scripts/make_cert.py`、先生画面 `web/teacher.js` の表示処理をそのまま使う。

差し替えたのは次のものだけである。

- NIC一覧の取得（OS境界）
- 番号入力（`builtins.input`）
- 待受の開始（HTTPはTestClientで代替）
- 先生画面の外側
  - DOM要素と `fetch` は、TestClientで得た実際のAPI応答を返す代役にした。
  - QR描画ライブラリは、渡された文字列を記録する代役にした。

このため、先生画面がブラウザからHTTPで取得すること、およびQR画像の実読み取りは確認していない（F2で確認する）。
先生画面の実行補助は `tests/teacher_page.py` に切り出し、既存の `tests/unit/test_teacher_network.py` と共用する。

- `test_operator_choice_matches_startup_api_qr_and_certificate`
  - 模擬構成: VMnet8 `192.168.74.1`（仮想）、Ethernet `192.168.1.42`（物理）、Wi-Fi `10.53.64.130`（物理）。
  - `--select-network` で一覧の3番（Wi-Fi）を選ぶ。次がすべて一致することを確認する。
    - 起動画面の公開IP・生徒用URL
    - 先生情報APIの `join_url`
    - 先生画面がQRへ渡す文字列と表示URL
  - 起動画面が案内した `make_cert.py --config … --advertise-ip … --force` を正規表現で取り出し、そのまま実行する。SANに実Wi-Fiが含まれ、仮想・別物理NICのIPが含まれないことを確認する。
  - 再起動後、起動画面とAPIの先生URLがともに `https://10.53.64.130:8443/teacher` になることを確認する。
  - 外部接続0件・外部名前解決0件。
- `test_restoring_saved_selection_setting_restores_previous_choice`
  - `advertise_ip: null` の設定を控える。仮想NICの固定に変えると、案内がその値に従う。
  - 控えた設定へ戻して再起動すると、変更前と同じ実Wi-Fiの自動選択に戻ることを確認する。
- `test_restored_ip_no_longer_present_is_not_used`
  - 戻した固定値 `10.53.64.130` が、IP変更後（Wi-Fiが `.131`）のNICに存在しない場合を模擬する。
  - 通常起動は終了コード1で止まり、旧IPのURLを表示しないことを確認する。
  - `--select-network` では候補一覧を表示する。Enterで中止すると、URLを出さずに終了することを確認する。

## 試験が不具合を検出できることの確認

本番コードを1か所ずつ一時的に壊し、下記の関連3ファイルを実行した。各回の後に `git checkout` で元に戻した。

| 破壊（sed） | 失敗した試験 |
|---|---|
| `server/network.py` の `candidates[number - 1]` を `candidates[0]` に変更（入力番号を無視） | `test_lan_ip.py::test_operator_selects_wifi_and_selection_is_revalidated`、`test_network_advertising.py::test_operator_choice_matches_startup_api_qr_and_certificate` |
| `web/teacher.js` のQRへ渡す `text: info.join_url` を `text: "x"` に変更 | `test_operator_choice_matches_startup_api_qr_and_certificate`、`test_teacher_network.py::test_teacher_clears_qr_and_url_when_network_becomes_invalid` |
| `server/network.py` の `role == "physical"` を `role != "none"` に変更（役割を無視） | `test_lan_ip.py` の4件、`test_restoring_saved_selection_setting_restores_previous_choice` |
| `server/main.py` の起動画面の生徒用URLを `127.0.0.1` に変更 | `test_startup_api_and_diagnostics_publish_same_selected_ip`、`test_operator_choice_matches_startup_api_qr_and_certificate` |

## 既存の試験（fa9fa9b）

- `tests/unit/test_lan_ip.py`
  - 社内10系＋仮想192.168系、複数物理NICでの選択要求を確認する。
  - 停止・リンクローカル・ループバック等の指定を拒否すること、指定IP消失、役割不明の扱いを確認する。
  - Windows列挙とCIM取得不能時の縮退を確認する。どちらも外部接続0件。入力待ち中に切断された場合は再検証する。
- `tests/integration/test_network_advertising.py`（既存5件）
  - 設定IPの消失と自動選択の変化で503を返し、`/healthz` は継続することを確認する。
  - 証明書CLIのSAN、起動・API・診断の一致、無効な指定を診断しないことを確認する。
- `tests/unit/test_teacher_network.py`: 503受信時に、先生画面のQR・URL・コードを消去することを確認する。

## 実行結果

```
.venv\Scripts\python -m pytest tests/unit/test_lan_ip.py tests/integration/test_network_advertising.py tests/unit/test_teacher_network.py -q
```

作業前は18件成功、追加後は21件成功。環境は Node.js v24.16.0、Python 3.12.10、Windows 11 Home 10.0.26200。

全体試験: 990件成功・5件スキップ（162秒）。実モデルを読み込む `tests/integration/test_real_mt.py`・`test_real_asr.py` は、本機ではモデル読込が試験の制限時間20秒を超え、プロセスごと中断するため除外した（本変更と無関係）。本作業の対象外の未コミット変更が作業ツリーにある状態で実行した。

## 開発PCのNIC列挙（読み取りのみ）

2026-09-23 22:14 JST、`scripts/network_interfaces.ps1` を実行した。
稼働中のIPv4は、WSL（Hyper-V）とVMware VMnet1・VMnet8が `virtual`、Wi-Fiが `physical` だった。停止中のリンクローカル3件は `virtual` と判定された。
自動採用はWi-Fiだった。現地で問題になったVMnet8（`192.168.74.1`）は、この開発PCでも仮想と判定され、採用されなかった。
自宅Wi-Fiでの結果であり、社内PCの列挙結果の代わりにはならない。

## 変更していないもの

- 本番コード、`config.yaml`、OSのネットワーク設定、FW、証明書（試験は一時ディレクトリだけを使用）。
- 別端末・社内ネットワークへの操作。

## #43への引き継ぎ

- **F2**: 当日の実Wi-Fi IP（`ipconfig`）を確認し、次の値と照合する。
  - `start.bat` 起動画面の「公開IP」と生徒用URL
  - 起動画面の一覧表示（複数候補の場合）と、担当者が選んだ番号
  - 先生画面の参加URL・QR読み取り先
  - `/api/teacher-info` の `join_url`・`teacher_url`
  - 起動画面が案内した `make_cert.py` の `--advertise-ip`、証明書SAN

  ローカル試験で一致を確認したのは模擬NICでの流れだけである。社内PCで各NICの役割が正しく判定されるかは未確認で、特に社内Wi-Fiアダプタが `physical` と判定されるかは現地で確認する。役割判定を誤った場合は、明示選択の手順で進め、列挙結果を #38 へ返す。
- **F7**: 設定を変更する前に `config.yaml` を控える。戻した後に再起動し、実Wi-Fi IPが選ばれること、URL、別端末でのページ到達を記録する。ローカルの復元試験は上記2件で済んでいる。戻した固定値がIP変更で無効になっていれば起動は止まる。その場合は再選択し、旧URLを配布していないことを確認する。
- **残課題**
  1. 社内PCでのNIC役割判定の実測。
  2. 別端末でのQR実読み取りと、ブラウザからの先生画面取得（本書はQRライブラリへ渡る文字列までを確認）。
  3. 復元後の実Wi-Fiでの到達。

  いずれも現地確認待ち。TLSの信頼・警告は #40（F4）、接続終了例外は #42（F6）で扱う。
