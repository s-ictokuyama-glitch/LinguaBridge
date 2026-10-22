# 相手側リセット後の正常終了の停止（Issue #50）

2026-09-29、開発用Windows PCのループバックで修正と試験を実施。現地Wi-Fi・対象端末は使っていない。
現地の症状と原因の推定は `docs/connection-field-results-2026-09-28.md` を参照。
本書の結果は**ローカルでの修正の証拠であり、現地での再確認（配信→Ctrl+C）は #43 で行う**。

仕様: https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/50

## 判定

| 項目 | 判定 | 根拠 |
|---|---|---|
| 後始末の `shutdown()` が相手側リセットで失敗した接続 | **修正済み** | ソケットを閉じ、asyncio の Server から外す。`wait_closed()` が戻る。ログは DEBUG に残す（当初は警告。2026-10-21 に変更、下記） |
| Ctrl+C 1回で HTTP・HTTPS の両方が終了 | **修正済み** | 共有ハンドラが両サーバーへ同時に終了を伝え、lifespan の終了（`pipeline.stop`）が走る |
| 後始末が戻らない接続が残った場合 | **上限あり** | 終了要求から10秒（`GRACEFUL_SHUTDOWN_S`）で残りを打ち切り、lifespan の終了へ進む |
| 現地の 10054 がこの経路であること | **未確定** | ループバックでは `shutdown()` は失敗せず、自然には再現しなかった（下記）。試験では失敗を注入した |

## 再現の試み

- 環境: Windows 11 Home 10.0.26200、Python 3.12.10、uvicorn 0.50.2、`ProactorEventLoop`。
- 平文・TLS の待受に対し、接続のみ／要求送信後／読み取り停止中／サーバー書き込み中／TLS 拒否の各条件で `SO_LINGER=0` の RST を送ったが、いずれもサーバー側の `shutdown()` は成功し、`wait_closed()` は戻った。
- そのため回帰試験では、RST で閉じた実接続の `shutdown()` だけに 10054／10053／WinError 64 を注入した。注入すると、既定のトランスポートでは `wait_closed()` が戻らない（現地の症状と同じ）ことを確認した。

## 対応

- **トランスポート**（`server/event_loop.py`）: `_ProactorBasePipeTransport._call_connection_lost` を写し、`shutdown()` の `OSError` が相手側リセット（#42 と同じ `is_peer_reset`）なら DEBUG ログ `切断済みの接続の後始末で shutdown が失敗したため、閉じて継続: <例外>` を残して close と `Server._detach()` まで進む。それ以外の `OSError` は既定どおり送出する（例外ハンドラへ報告され、接続は残る）。平文・TLS の両方で使うよう、`_make_socket_transport` と `_make_ssl_transport` を差し替えた。
  - このログは当初は警告だった。2026-10-21 の #52 の別PCでの再確認では、授業中に同じ警告が数十行続いた。ブラウザが接続を RST で閉じるたびに起きる平常の出来事で、字幕の配信にも影響しなかった。先生のコンソールを埋めないよう DEBUG に下げた。サーバーは INFO で起動するので、通常は表示されない。接続確立前のリセット（#42、`接続確立前に相手側が切断したため…`）は警告のまま。
- **シグナル**（`server/main.py` の `_serve`）: uvicorn 0.50 の `serve()` は `capture_signals()` で自前の SIGINT/SIGTERM ハンドラを入れるため、従来の `install_signal_handlers` の上書き（0.50 には存在しない）は効いていなかった。各サーバーの `capture_signals` を無効化し、共有ハンドラで全サーバーへ同時に `should_exit` を伝える。2回目以降の Ctrl+C も同じ扱いで、uvicorn の `force_exit` にはしない（`force_exit` では lifespan の終了＝`pipeline.stop`・授業記録の確定が飛ぶため）。
- **終了待ちの上限**（意図した挙動変更）: 両サーバーの `timeout_graceful_shutdown` を10秒にした。uvicorn は最後の `wait_closed()` を期限なしで待ち、`force_exit` でも解けない。上限はその保険で、超えると残りのタスクを取り消して lifespan の終了へ進む。リセットが無い通常の終了でも、10秒を超えて続く要求・WS接続の処理は取り消される（WS接続は終了要求の時点でサーバーから閉じるので、通常は上限に達しない）。
- 写した CPython 関数（`_call_connection_lost` は 3.12.10 と 3.14.2、`_make_ssl_transport`）のソースハッシュを試験で照合する。3.13 以降は `server._detach(self)` と接続を渡すので、写しは版で呼び分ける。

## 戻しスイッチ（`LINGUABRIDGE_STOCK_EVENT_LOOP=1`）との関係

- 戻しスイッチは #42 の accept 修正と本件のトランスポート修正を**まとめて**既定に戻す。戻した状態では、後始末の `shutdown()` が相手側リセットで失敗した接続が再び Server に残る。
- シグナル処理と終了待ちの上限は `_serve` 側の変更なので、戻しスイッチの影響を受けない。戻した状態でも Ctrl+C 1回で HTTP 側（lifespan）は即座に終了し、HTTPS 側は最長10秒で打ち切られて終了する（試験で確認）。
- 戻し方・解除の手順は `docs/connection-reset-2026-09-23.md` の「以前の状態へ戻す手順」と同じ。

## 回帰試験

`tests/integration/test_shutdown_after_reset.py`（Windowsのみ）

- `test_failed_shutdown_after_peer_reset_still_detaches`: 平文／TLS × 10054／10053／WinError 64。RST で閉じた実接続の `shutdown()` が失敗しても、`wait_closed()` が3秒以内に戻り、接続数は0、例外ハンドラへの報告はなく、警告ログにWinErrorが残る。
- `test_other_shutdown_errors_stay_visible`: 相手側リセット以外（WSAEINVAL）は既定どおり例外ハンドラへ報告される。
- `test_rollback_switch_restores_stock_transport`: 戻しスイッチで既定ループに戻り、元の障害（`wait_closed()` が戻らない）が再発する。
- `test_stdlib_originals_unchanged`: 写し元の CPython ソースが変わっていない。
- `test_one_ctrl_c_stops_both_listeners_after_reset`: 本番の二重待受（`main._serve`、実TLS）をメインスレッドで動かし、HTTPS へ RST 接続を作ってから `signal.raise_signal(SIGINT)` を1回送る。上限より前に両待受が閉じ、`pipeline.stop` が呼ばれる。
- `test_rollback_switch_still_stops_within_graceful_limit`: 戻しスイッチ・上限2秒で同じ操作をすると、HTTP 側の lifespan 終了は1.5秒未満で始まり、全体は上限（2秒）以上かかって終了する。修正前のシグナル処理では HTTP 側の終了が HTTPS 側の打ち切り後（約2.4秒）まで遅れ、この試験は失敗した。

## #43への引き継ぎ

- **F7**: 現地で配信 → Ctrl+C を行い、終了案内が出て 8000・8443 の両待受が解放されること、授業記録ONなら記録が確定することを確認する。直前に 10054 のログ（修正後は警告 `切断済みの接続の後始末で shutdown が失敗…`）が出ていたかも記録する。2026-10-21 以降の版ではこのログは DEBUG で、起動ウィンドウには出ない。
- 戻しスイッチを試す場合、Ctrl+C から終了まで最長10秒かかることがあるのは想定どおり。
