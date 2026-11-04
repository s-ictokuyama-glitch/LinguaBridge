# 学校ネットワーク入口と参加制限の性質

`school_access.py` は Python 3.12.10 で製品の関数と実アプリを直接呼ぶ有限検証器である。独立したTLA+モデルや、SMTによる全入力の証明ではない。ASGI・標準ライブラリのIP判定・参加制限の状態を別言語で再実装するずれを避けるため、既存の実コード検査と同じ方式を選んだ。依存は `school_access_requirements.txt` に固定する。

仕様は `CONTEXT.md` と ADR 0003。既存の `test_source_access.py`、`test_ws_boundary.py`、`test_rate_limit.py`、先生情報APIの関連テストも照合した。既存テストは補助的な回帰検査であり、以下の全列挙と破壊検出の代替にはしない。

## 製品コードとの対応

| 性質 | 製品コード | 検査・観測点 | 破れたときの影響 |
|---|---|---|---|
| 解析できない送信元と `is_global` が真の送信元を拒否 | `source_access.socket_ip`、`SourceAccess.__call__` | `source_parse`、`source_gate`、`source_denial`。後段呼出しと403・1008 | 外部送信元が処理へ到達する |
| 許可送信元だけがHTTP・静的配信・WSへ進む | `main.create_app` のミドルウェア登録と mount、`ws_endpoint` | `source_wiring`、`source_routing`。実ページ・API・静的ファイル・WSと実ルーター入口 | 組込み漏れでガードを迂回する |
| 転送情報でソケットの送信元を変更しない | `SourceAccess.__call__`、`serving.start_listeners` の `uvicorn.Config` | `source_gate`、`listener_proxy`、`listener_source`。HTTP/HTTPSの実Configと実受信scope | 偽装ヘッダーが入口判定を変える |
| 先生コードは許可送信元かつループバックまたはソケット接続先と同一IPだけ | `main.create_app.teacher_info`、`source_access.socket_ip` | `teacher_disclosure`、`teacher_body`。実アプリのstatus、JSONキー・値、本文 | 別端末に先生権限の秘密が漏れる |
| 役割別失敗を同一IPで合計し、5回で60秒制限 | `Lesson.join`、`JoinRateLimiter.record_failure` | `limit_accounting`、`join_outcome` | コード総当たりが続けられる |
| 成功時は同じ役割だけ消す | `Lesson.join` の成功呼出し、`JoinRateLimiter.record_success` | `limit_accounting`。両役割の失敗数を操作ごとに比較 | 生徒参加を繰返して先生コードの失敗を消せる |
| 制限中は正解も拒否し、解除時刻ちょうどから再試行 | `Lesson.join` の最初の制限判定、`JoinRateLimiter.is_blocked` | `join_outcome`、`join_role`、`limit_accounting` | 制限の迂回または正当な参加の拒否 |
| IPごとの回数・制限時刻は独立 | 同上のIP辞書、`main.ws_endpoint` → `Connection.receive` → `Lesson.join` | `limit_accounting`、`join_wiring` | 他端末の制限解除や巻き添え拒否 |

`SourceAccess` とルーターに後段の観測関数を置く検査に加え、`create_app` の実ルートを呼ぶ。静的ファイルは実 `web/teacher.js`、ページは `/`、APIは `/healthz` と `/api/teacher-info`、WSは `/ws`。拒否のstatusだけで後段へ到達していないと推測しない。

## IPとリクエストの有限入力

`addresses()` は161個の入力を生成する。分類は次のとおり。

- 送信元なし、空文字、ホスト名、壊れたIPv4、括弧付きIPv6、省略IPv4、先頭ゼロ付きIPv4を含む解析失敗候補。
- IPv4のプライベート、共有、ループバック、リンクローカル、文書用、ベンチマーク、マルチキャスト、予約領域。
- IPv6の未指定、ループバック、NAT64、破棄用、特殊割当、文書用、6to4、ULA、リンクローカル、マルチキャスト、ゾーンID付きアドレス。
- `addresses()` に列挙した23個のCIDRそれぞれの先頭・末尾・直前・直後。IPv4の `192.0.0.9` と `.10`、IPv6の特殊割当内の例外候補も追加する。
- 解析可能な各IPv4代表値を `::ffff:` でIPv4射影IPv6へ変換した値。期待値は射影先へ独自に正規化せず、Python 3.12.10 の `ipaddress` がその値に返す属性に従う。

許可の期待値は「解析成功かつ `not is_global`」。`is_private` やRFC1918に置き換えない。標準ライブラリのIP分類を信頼する。CIDR一覧は標準ライブラリの全分類表を網羅するものではない。代表値と境界で製品の分岐を確認する検査であり、全IPv4・IPv6空間、すべての不正文字列、将来のPython版への保証を主張しない。標準ライブラリの分類が変われば、許可される値も変わり得る。

ヘッダーは3組。なし、ループバックを偽る組、グローバル送信元を偽る組を使う。Host、Forwarded、X-Forwarded-For、X-Real-IP、X-Forwarded-Host、X-Forwarded-Protoを同時に変える。異常な生HTTP構文、全ヘッダー値、個々のヘッダーの全組合せは探索しない。

先生情報APIでは、各送信元に対して接続先11候補を使う。なし、不正、ワイルドカード、別のIPv4、IPv4/IPv6ループバック、リンクローカル、射影IPv6、文書用IPv6、グローバルIPv4、送信元と同一表記の値。HTTP/HTTPSと3ヘッダー組を直積にする。送信元の入口拒否が公開条件より優先し、グローバルIP同士が一致しても403で先生コードを含まない。異なるIPv6表記やゾーンIDの一般的同値性の全探索ではない。

3,703件の送信元検査と10,626件の公開条件検査を行う。ASGI scope の `client` と `server` はタプルまたはNoneで、キーの欠落は同じ `scope.get` 分岐としてソースを照合する。ASGI仕様外の非文字列ホストや壊れたタプルは対象外。

## 参加制限の状態探索

実 `Lesson.join` と実 `JoinRateLimiter` に既定値5回・60秒を使う。期待値は2IPそれぞれについて、生徒・先生の失敗数、解除時刻、参加中の役割を持つ。時刻は全IP共通で単調増加する。

1操作は、2IP × 2役割 × 正誤コードの8種類の参加試行、または次の時刻への進行。時刻列は `0, 59.9, 60.0, 60.1, 120.1` 秒で、途中の時刻を飛ばさない。すべての操作列を長さ0〜8まで、幅優先・同一状態の統合によって調べる。

状態の同一性は、両IPの役割別失敗数・解除時刻・参加役割・時刻インデックスすべての一致で定義する。製品状態の回数・時刻と期待値を各遷移で照合した後で統合する。同じ状態の後続は同じ認証結果になるため、最短到達列だけを保存し、同一状態への長い履歴の再展開を省く。4,891状態・28,586遷移となる。深さ8に到達した状態の後続は探索しない。

各参加試行は `joined`、`bad_code`、`rate_limited` の応答、参加役割、両IPの全失敗数・全解除時刻を比較する。誤役割での成功消去、合計でなく役割別の閾値、他IPへの書込みも、将来の拒否を待たずに検出する。成功は同じ役割の数だけをゼロにし、上限到達はそのIPの蓄積を消して制限時刻を設定する。制限中の試行は時刻を延長しない。これは現行実装の状態と照合した性質である。

固定時刻列だけでは全解除時刻の前後を覆えない。そのため別に、開始時刻5値 × 5失敗の役割列32通り × 解除時刻の0.1秒前・ちょうど・0.1秒後 × 正解で再試行する2役割の960ケースを実行する。時刻は注入した時計で固定し、各参加処理中には進めない。浮動小数点の全表現、時計の逆行・NaN・無限大、実時間の経過は対象外。

名簿への追加・送信は観測関数へ置き換える。実 `Lesson` を構築し、参加処理はコード照合から認証成功・拒否の観測点まで実関数を通す。各IPにつき1接続を持ち、同じ接続の生徒・先生の切替と拒否時の役割保持を調べる。名簿の他IP先生の交代・実送信キューは状態に含めない。実名簿・実Connectionを通るWebSocket組込み13ケースで、生徒成功を挟んだ先生失敗5回、制限中の正解、別IPの正解、解除時刻の再試行を補う。

`Lesson.join` の制限判定・照合・回数更新にはawaitがなく、同一イベントループでは最初の送信または名簿追加のawaitまで不可分である。同期の `JoinRateLimiter` も同じ条件。認証後の名簿追加の待機との交錯、時計が処理中に境界をまたぐ場合、マルチスレッド・複数プロセスの共有、再起動は探索しない。先生交代のawait順序は既存の `teacher-lifecycle` と `ws-teacher-ingress` の対象である。

## リスナーと代替処理

実 `start_listeners` で127.0.0.1の空きポートにHTTPとHTTPSを立てる。HTTPSは一時ディレクトリに生成した証明書を信頼して接続する。両リスナーの実 `uvicorn.Config.proxy_headers` がFalseであることと、3ヘッダー組すべてで観測したclientが127.0.0.1のまま、schemeがHTTP/HTTPSそれぞれのままであることを確認する。Configと受信scopeの両方を調べる。

リスナーのアプリはscope観測関数。製品アプリの入口・公開条件は前述のASGI検査で別に通す。`main._serve` が両リスナーを組み立てて `start_listeners` へ渡すことはソースで照合した。実ネットワークでグローバル送信元やIPv6を割り当てる検査、TLS信頼配布、OSファイアウォール、リバースプロキシ、Originの認可、証明書のライフサイクルは対象外。

ASGI検査はASR/MTフェイクとenergy VADを使い、lifespanを開始しない。実Webファイルは読むが音声・翻訳・記録処理を起動しない。`Publication.current` はNICの状態に依存しない固定値にする。ネットワークの変化による503・公開URL生成の正しさは対象外。

## 破壊検出と結果

`--mutation-check` は `server/` と `web/` を一時コピーし、`school_access_mutations.py` の30種類の編集を1個ずつ適用して別プロセスで対象検査群を実行する。`--product-root` を最優先のimport先にし、変更した製品を直接呼ぶ。編集箇所はちょうど1件の一致を要求する。通常の作業ツリーは変更しない。

| 破壊対象 | 件数 | 内容 |
|---|---:|---|
| 送信元と組込み | 7 | ガード削除、global反転、送信元なしの許可、転送情報の採用、WS除外、ミドルウェア削除、静的配信の迂回 |
| リスナー | 1 | proxy_headersをTrueへ変更 |
| 先生コード公開 | 9 | 条件削除・反転、非global全許可、HTTPS全許可、Host採用、転送情報採用、ループバック除外、同一IP除外、入口削除 |
| 参加制限とWSのIP | 13 | 加算削除、役割別閾値、閾値遅延、全役割消去・消去漏れ、期限の前倒し・遅延・短縮、制限判定削除、成功・失敗の役割省略、共有IPキー、WSのIP共有 |

検出成功には終了コード1と、各破壊に指定した `violation.property` の両方を要求する。クラッシュ、タイムアウト、置換不一致では検出成功にならない。破壊検出後は元の実装の全検査を再実行し、`after_mutations.status: PASS` も完了条件にする。

同期対象は `a9d1d82b86db138c7235ea47b4d6d8a3a98f76a7`。保存した通常版は全群PASS、破壊版は30件すべて指定の性質でFAIL。製品の不具合は確認されなかった。初回の観測関数を `router.app` へ置く方法は、Starletteが構築済みmiddleware_stackに保持した参照に届かず、許可済み入力にも未到達と報告した。実ルートの成功と実Routerの呼出しを照合し、観測点を `router.middleware_stack` に修正した。これは検証器の誤りで、製品のガードは変更していない。

再実行と結果の読取りは [school-access スキル](../../.agents/skills/school-access/SKILL.md)、通常版・破壊版の具体的反例は [school_access_baseline.json](school_access_baseline.json) を参照する。既存スキルの拡張実行は [ws_teacher_ingress_school_access_baseline.json](ws_teacher_ingress_school_access_baseline.json) に保存した。260シナリオと従来の7破壊、参加制限の全検査と13破壊がすべて期待どおりとなった。

補助的な回帰検査は次の73件が通過した。

```powershell
.venv/Scripts/python.exe -X utf8 -m pytest -q tests/unit/test_rate_limit.py tests/integration/test_source_access.py tests/integration/test_ws_boundary.py tests/unit/test_publication.py
# 37 passed
.venv/Scripts/python.exe -X utf8 -m pytest -q tests/integration/test_ops.py::TestTeacherInfoAccess tests/unit/test_lesson.py
# 36 passed
```
