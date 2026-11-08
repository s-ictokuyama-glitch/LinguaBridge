# 接続の案内

先生・生徒は「接続に困ったとき」から、接続確認と設定担当者へ渡す記録の案内を開ける。

## Sub-features

- `help-entry` は先生・生徒のリンクから `/connection-help` を開く。
- `help-address` は起動設定のポートと起動時に選んだ接続先IPを案内する。
- `help-diagnostics` は設定担当者向けの診断手順と記録欄を展開する。

## How to get to it (user POV)

先生ページ、または生徒の参加画面で「接続に困ったとき」を押す。「設定担当者向け：診断と次の操作」を開く。

## Driving it with Playwright

Preconditions:

- 所有runのdoctorが成功している。先生のページ遷移は配信前に行い、終了後は元の `/teacher` へ戻す。

- **入口。** 先生・生徒の各ページで `get_by_role('link', name='接続に困ったとき').click()`。URLが `/connection-help`、`get_by_role('heading', name='Live Translation - 接続に困ったとき', exact=True)` が表示されることを確認する。
- **案内。** `page.locator('summary').click()`。loopbackの `/healthz` のリンク先を `instance.json.port` と照合する。別端末向けのHTTP・HTTPSリンクを起動引数の `--advertise-ip` と実効ポートに照合する。`start.bat --diagnose` の共通案内と記録欄があり、`{{...}}` が残っていないことを確認する。詳細コマンドの `-m server.diagnostics` は、開発版では `.venv`、配布版では同梱Pythonを使う。

## Gotchas

- このページを開いても診断コマンドは実行されない。ページ表示とネットワーク到達性の成功を分けて記録する。
- 起動時に選んだNICとIPの組が使えなくなると、再起動まで503となる。別端末向けリンクは消え、サーバーPC内の確認手順が残る。この分岐は専用環境で選択したNICまたはIPを失わせて確認する。
- 別端末HTTPS、証明書警告、学校LAN、配布パッケージの案内は、対応する端末・起動形態で確認する。開発機のloopback表示だけで合格としない。
