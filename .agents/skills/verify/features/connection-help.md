# 接続の案内

先生・生徒は「接続に困ったとき」から、接続確認と設定担当者へ渡す記録の案内を開ける。

## Sub-features

- `help-entry` は先生・生徒のリンクから `/connection-help` を開く。
- `help-address` は起動設定のポートと現在の接続先IPを案内する。
- `help-diagnostics` は設定担当者向けの診断手順と記録欄を展開する。

## How to get to it (user POV)

先生ページ、または生徒の参加画面で「接続に困ったとき」を押す。「設定担当者向け：診断と次の操作」を開く。

## Driving it with Playwright

Preconditions:

- 所有runのdoctorが成功している。先生のページ遷移は配信前に行い、終了後は元の `/teacher` へ戻す。

- **入口。** 先生・生徒の各ページで `get_by_role('link', name='接続に困ったとき').click()`。URLが `/connection-help`、見出しが「接続に困ったとき」であることを確認する。
- **案内。** `page.locator('summary').click()`。本文のloopback `/healthz` リンクのポートを `instance.json.port` と照合する。実NICの案内URL、`start.bat --diagnose`、記録欄を読み、`{{...}}` の未置換が無いことを確認する。開発機では `.venv`、配布パッケージでは同梱Pythonを使う案内になっているか、対象の起動形態で確認する。

## Gotchas

- このページを開いても診断コマンドは実行されない。ページ表示とネットワーク到達性の成功を分けて記録する。
- IPを取得できない場合は503となり、別端末用URLを出さずサーバーPC内の確認手順を示す。この分岐の確認にはIP取得不能な専用設定・環境が必要。
- 別端末HTTPS、証明書警告、学校LAN、配布パッケージの案内は、対応する端末・起動形態で確認する。開発機のloopback表示だけで合格としない。
