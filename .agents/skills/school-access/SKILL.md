---
name: school-access
description: 送信元制限と先生コード公開条件を製品コードの有界探索で確認する。Use when changing SourceAccess, socket_ip, create_app middleware or teacher_info, start_listeners proxy_headers, or school access policy. JoinRateLimiter and Lesson.join admission checks are also covered and shared with ws-teacher-ingress.
---

# 送信元と先生コードの公開を検証する

解析できない送信元と `is_global` が真の送信元は、HTTP・静的配信・WebSocket の後段へ到達できない。先生コードは、許可された送信元がループバックか、ソケットの接続先IPと一致するときだけ返す。参加制限はIP別・役割別の失敗を保持する。これらが破れると、別端末への先生コード漏えいやコード総当たりにつながる。

## 実行する

1. [CONTEXT.md](../../../CONTEXT.md)、[ADR 0003](../../../docs/adr/0003-school-access-and-teacher-code.md)、[性質と探索範囲](../../../scripts/formal/school_access.md)を読み、変更箇所が対応表のどこに当たるか確認する。
2. リポジトリ直下で次を実行する。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/school_access_requirements.txt python -X utf8 -B scripts/formal/school_access.py --mutation-check
```

同じ版の依存がある環境では `.venv/Scripts/python.exe -X utf8 -B scripts/formal/school_access.py --mutation-check` でも実行できる。Windows の制限環境で asyncio の内部 socketpair 初期化が停止する場合は、通常のローカル通信が許可された環境で実行する。

3. 終了コード0、`status: PASS`、5検査群の件数、30件の破壊すべての `detected: true`、破壊検出後の `after_mutations.status: PASS` を確認する。通常版は送信元3,703ケース、公開条件10,626ケース、参加制限4,891状態・28,586遷移と解除境界960ケース、WebSocket 組込み13ケース、実HTTP/HTTPSリスナー6リクエストを調べる。[保存結果](../../../scripts/formal/school_access_baseline.json)と比較する。既存 pytest の通過だけで完了としない。

部分再実行には `--group source`、`disclosure`、`limits`、`join-wiring`、`listeners` を使える。`--mutation-check` も併用できるが、部分実行は全体の完了条件を満たさない。参加制限を含む WebSocket 認可の変更では、[ws-teacher-ingress](../ws-teacher-ingress/SKILL.md) の拡張コマンドも使う。

## 反例を読む

`violation.property` と `trace` が、製品コードへ実際に渡した入力と順序を示す。`expected` と `actual` を比較する。破壊版では、先生コードの失敗後に生徒の参加成功で両役割を消すと `limit_accounting` が失敗する。入口ミドルウェアを外すと、送信元なしのHTTPが通り `source_wiring` が失敗する。

通常版の反例は、同じ `--group` で再実行し、[製品コード対応表](../../../scripts/formal/school_access.md)にある関数へ同じ入力を渡して再現する。ASGI scope とソケット情報、時計、名簿の代替処理が実装の境界を表しているか先に確認する。検証器のずれなら検証器を直す。製品の不具合なら、入力手順と影響を報告し、性質を弱めたり製品を修正したりして通過させない。置換の不一致、例外、非JSON出力、タイムアウトは破壊検出の成功として扱わない。

## 同期を保つ

同期コミットは `5381edb92062d1a35972b3a7dee8d387c5b0aa1b`。対応する製品ファイルは `server/source_access.py`、`main.py`、`serving.py`、`rate_limit.py`、`lesson.py`、呼出し経路の `connection.py`。関数単位の対応と原子性は[性質の定義](../../../scripts/formal/school_access.md)に置く。

変更時はガードの位置、await、時計の読取り、ASGI の送信元、Uvicorn 設定を照合する。検査対象の分岐が増えたら入力分類と破壊版も追加する。通常版と対応する破壊版の結果が意図どおりになった後で、このファイルと検証器の `SYNC_COMMIT` を進める。追随作業には `/maintain-formal-verification-skill` を使える。