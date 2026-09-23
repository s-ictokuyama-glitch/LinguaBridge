# 接続リセットのローカル再現・影響評価（Issue #42）

2026-09-23、開発用Windows PCのループバックで実施。現地Wi-Fi・対象端末は使っていない。
本書の結果は**ローカルでの再現と修正の証拠であり、現地で観測された10054と同一の事象であることは未確認**。現地での照合は #43 F6・F7・F8 で行う。

仕様: https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/42

## 判定

| 項目 | 判定 | 根拠 |
|---|---|---|
| WS接続中（参加済み）の生徒・先生のRST | **影響なし** | ws／wss × 生徒／先生 × 各3回。サーバーは `ConnectionResetError`（WinError 64）を受け、利用者・送信キューを解放。死活・新規参加・残存生徒への字幕・取りこぼし再送・Origin拒否・正常終了・待受閉鎖とも正常 |
| 接続確立前（accept完了前）のRST | **再現・修正済み** | 既定のProactorループでは `Accept failed on a socket`（WinError 64）の1件で**その待受が閉じ、以後の新規接続がすべて失敗**。局所対応で待受継続を確認 |
| accept失敗時の受け側ソケット | **修正済み** | 既定では受け側ソケットが閉じられず `Task exception was never retrieved` が出る。相手側リセット時は閉じるよう変更 |
| WSハンドシェイク中のRST（accept直前の切断） | **影響なし（ログのみ）・未修正** | uvicorn が `Exception in ASGI application` / `RuntimeError: Expected ASGI message 'websocket.send' or 'websocket.close', but got 'websocket.accept'` を出す。接続数は0に戻り待受も継続。uvicorn側の挙動のためアプリでは抑制しない |
| 現地の10054との同一性 | **未確定** | 今回のローカル再現で出たのはWinError 64。10054の現地経路は #43 F6 で照合する |

## 再現した障害

- 環境: Windows 11 Home 10.0.26200、Python 3.12.10（MSC v.1943 64 bit）、uvicorn 0.50.2（WS実装 websockets-sansio）、websockets 16.0、`ProactorEventLoop`。アプリは `d0e9182` ＋本変更。推論はフェイク、証明書は一時生成、待受・クライアントとも127.0.0.1。
- 手順: 本番と同じ `server.main._serve` の二重待受（HTTP／HTTPS）を起動する。別スレッドの生ソケットから19パターン（接続のみ、要求送信直後、応答後、`Connection: close`、ページ途中、WSハンドシェイク直後、WS確立後、Origin不許可、TLSの途中）を各20回実行し、`SO_LINGER=0` で閉じてRSTを送る。
- 既定ループ（3回中3回）: 平文待受の「WSハンドシェイク送信直後にRST」で次が出た。
  ```
  Accept failed on a socket
  OSError: [WinError 64] 指定されたネットワーク名は利用できません。
  Task exception was never retrieved  (IocpProactor.accept.<locals>.accept_coro, 同じ WinError 64)
  ```
  直後から、同じ待受への以後のパターンはすべて接続タイムアウトになった。HTTPS待受は別ソケットのため継続した。サーバープロセスは生きたままで、停止要求では正常終了した。
- 原因: CPythonの `ProactorEventLoop._start_serving` は、accept中の `OSError` を種類を問わず待受の故障とみなし、例外ハンドラへ報告したうえで待受ソケットを閉じる。相手がAcceptEx完了前にRSTを送ると、この経路に入る。Python 3.14の同ファイルも同一実装であることを確認済み。

## 対応の選択

- **ランタイム更新は不採用**。3.12→3.14で該当コードに差がなく、更新しても解消しない。
- **SelectorEventLoopへの切替は不採用**。Windowsの `select` の上限と、TLS・WS経路全体の挙動変化が大きい。
- **局所対応を採用**（`server/event_loop.py`）。相手側リセットとみなすのは `ConnectionResetError`、`ConnectionAbortedError`、WinError 64／1236／10053／10054 だけ。その場合は警告ログ `接続確立前に相手側が切断したため、この1件を破棄して待受を継続: <例外>` を出してacceptを続け、受け側ソケットを閉じる。それ以外のaccept失敗は、既定どおり例外ハンドラへ `Accept failed on a socket` として報告し、待受を閉じる（試験で確認）。既知の例外を非表示にするのではなく、ログに残したまま待受の停止だけを防ぐ。
- 起動処理の変更は `server/main.py` で `asyncio.run` を `event_loop.run` に置き換えた1行のみ。
- 相手側リセットとして扱うのは、**完了したacceptの結果**が失敗した場合だけ。accept発行そのものの失敗は既定どおり報告・待受停止とし、再試行の空転を起こさない（試験で確認）。
- 写したCPython関数（`BaseProactorEventLoop._start_serving`、`IocpProactor.accept`）のソースハッシュを試験で照合する。ランタイム更新で元が変わると試験が失敗し、写しの見直しを促す。3.12.10と3.14.2で同一を確認済み。
- 対象コードの選定: 再現したのはWinError 64のみ。WSAECONNRESET(10054)、ERROR_CONNECTION_ABORTED(1236)、WSAECONNABORTED(10053) は、いずれも接続1件の中断を表し、待受自体の故障ではないため同じ扱いとした。これらは注入試験でのみ確認しており、実レースでの発生は未確認。

## 以前の状態へ戻す手順

1. サーバーを停止する（Ctrl+C）。
2. 同じPowerShellで `$env:LINGUABRIDGE_STOCK_EVENT_LOOP = "1"` を設定し、`start.bat` から再起動する。これで既定の `ProactorEventLoop` に戻る。
3. 解除するには、`Remove-Item Env:LINGUABRIDGE_STOCK_EVENT_LOOP` の後に再起動する。コードごと戻す場合は本変更のコミットを `git revert` する。

ローカルでの復元確認:

- プローブ本体とログは、コミット対象外の `.tmp/issue42-evidence/` に置いた（`probe_reset.py`、`probe-{fixed,stock}-*.log`）。
- `tests/integration/test_accept_reset.py::test_rollback_switch_restores_stock_loop`: 変数を設定すると既定ループに戻り、元の障害（待受の閉鎖）が再発することを確認。
- 上記プローブを変数設定で3回実行し、3回とも元の障害が再現した。未設定の修正版では5回とも（最終実装で1回を含む）全パターンで接続拒否0・ループ例外0・接続残0・正常終了。修正版では、accept時の相手側リセットが実行ごとに2〜12件、警告として観測された。

## 回帰試験

- `tests/integration/test_accept_reset.py`（Windowsのみ）
  - accept時に WinError 64／10054／1236 を1回だけ注入しても、実接続を受け付けて応答できる。例外ハンドラへの報告はなく、警告ログには残る。
  - 相手側リセット以外（WSAEMFILE）は、既定どおり例外ハンドラへ報告され、待受が閉じる。
  - 戻しスイッチで既定ループと元の障害が戻る。
  - accept発行時の失敗は再試行しない（既定どおり）。
  - 写し元のCPythonソースが変わっていない。
- `tests/integration/test_connection_reset.py`（本番の二重待受・本番ループ・実ソケット／TLS）
  - `test_reset_preserves_service_and_releases_resources`: 参加済みの生徒・先生をRSTで切断する（ws／wss、各3回）。試行時刻、サーバー側の観測時刻、段階、例外全文、実行環境の版を `record_property` に記録する。
  - `test_reset_before_accept_keeps_both_listeners`: 両待受へ「接続→送信→即RST」を各200回送った後も、両待受の `/healthz`・参加・資源解放・正常終了が成り立つ。この試験は実レースに依存する。修正前（accept_coro変更前）には `Task exception was never retrieved` の検出で失敗した。accept時の相手側リセットが実際に起きたかどうかは記録するが、合否の条件にはしない（決定的な確認は上の注入試験が担う）。

証拠を記録として出力するには次を実行する。

```
.venv\Scripts\python -m pytest tests/integration/test_connection_reset.py tests/integration/test_accept_reset.py -o junit_family=legacy --junitxml=<出力先>/junit.xml
```

記録例（ws・生徒、1回目）: `reset_at`／`observed_at` とも `2026-09-23T10:25:01.620092+00:00`、段階 `ws open, student joined, session live`、サーバー例外 `ConnectionResetError(22, '指定されたネットワーク名は利用できません。', None, 64, None)`。実行環境の版は Python 3.12.10、Windows-11-10.0.26200、uvicorn 0.50.2、websockets 16.0、h11 0.16.0。同じ実行の200回burstでは、accept時の相手側リセットが1件、警告として記録された。

## #43への引き継ぎ

- **F6**: 現地で10054等が出たら、端末・直前操作・時刻・段階を照合する。次の区別も記録する。
  - `Accept failed on a socket`: 本書の障害。修正後は警告ログ `接続確立前に相手側が切断…` に変わる。
  - 接続中の切断: 影響なし。
  - uvicornのASGI RuntimeError: ログのみ。
  - それ以外の例外: 未確定として残す。
  ローカルで出たのはWinError 64で、10054そのものは再現していない。現地で発生しなかった場合は「この試行では非再現」とし、原因が確定したとはしない。修正後もHTTP・HTTPSの両待受に新規接続できることを、切断直後の `/healthz` で確認する。
- **F7**: 起動処理を変更したため、F7の「ランタイム・起動処理を変更した場合」に該当する。現地では戻しスイッチの適用→再起動→既定ループで起動できることの確認、そして解除後の再診断を行う（必要と判断した場合のみ）。ローカルでは `event_loop.run` を直接使って戻しを確認した。`start.bat`→`run.ps1`→`server.main` を通した起動経路での戻しは未確認（`main()` は同じ `event_loop.run` を呼ぶ）。
- **F8**: 残課題は2点。(1) 現地10054の経路は未確定。(2) uvicornのaccept前切断によるERRORログは未修正で、影響はログのみ。修正済みはローカルのaccept停止だけで、現地での復旧合格とは読み替えない。
