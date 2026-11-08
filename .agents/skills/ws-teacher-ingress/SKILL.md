---
name: ws-teacher-ingress
description: 現在の先生だけが WebSocket から音声・授業操作・記録切替を入力できることを実コードの有界探索で確認する。Use when changing Connection.receive, its audio byte limit, Lesson.join role changes or join rate limits, or Roster.add detachment during teacher replacement.
---

# 現在の先生だけに WebSocket 入力を許可する

`/ws` は、先生コードで参加した現在の先生の上限内の音声、授業操作、記録切替だけを対応する処理へ一度ずつ渡す。違反すると、生徒や交代済みの先生の入力で字幕、授業の進行、記録が変わる。

## 実行する

リポジトリ直下で実行する。Connection.receive にテキストと音声を渡し、Lesson と参加者名簿を通る認可を確認する。WebSocket アダプターは pytest の境界テストで確かめる。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/ws_teacher_ingress.py --mutation-check
```

同じ版の依存がある環境では `.venv/Scripts/python.exe -X utf8 -B scripts/formal/ws_teacher_ingress.py --mutation-check` も使える。Windows の制限環境で asyncio が起動時に停止する場合は、内部 socketpair の実行権限を確認する。

終了コード 0、`status: PASS`、`complete_scenarios: 260`、7 件の破壊すべての `detected: true` を確認する。破壊版は `connection.py` と `lesson.py` の一時コピーを使う。`student_code_as_teacher` では、生徒用の参加コードで先生として参加を試みた直後に音声を渡すと `feed_audio` が呼ばれる。

送信元の制限、役割違いのコードに対する応答、参加制限をまたぐ再試行を変更したときは、境界テストも実行する。

参加の失敗回数・制限時間・役割別消去・IPの引渡しを変更したときは、次の拡張検査を実行する。認可の260シナリオに加え、実 `Lesson.join` と `JoinRateLimiter` の4,891状態・28,586遷移、解除時刻の960ケース、実WebSocketの13ケース、参加制限に対する13件の破壊検出を確認する。参加制限部分の同期コミットは `5381edb92062d1a35972b3a7dee8d387c5b0aa1b`。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/school_access_requirements.txt python -X utf8 -B scripts/formal/ws_teacher_ingress.py --mutation-check --join-limit-check
```

既存の短い履歴だけでは失敗上限に達しない。拡張の性質・時刻・原子性・代替する後段は [学校ネットワーク入口と参加制限](../../../scripts/formal/school_access.md) を参照する。送信元制限と先生コード公開条件を変えた場合は [school-access](../school-access/SKILL.md) も実行する。

```powershell
.venv/Scripts/python.exe -m pytest -q tests/integration/test_ws_boundary.py tests/integration/test_source_access.py
```

## 結果を読む

- 参加履歴は、先生・生徒の各役割と、先生コード・生徒用の参加コード・どちらとも異なるコードの組合せを長さ0〜3で全列挙する。役割に合うコードを受理したときだけ役割を更新する。259履歴を調べる。
- 先生交代は2接続で1回行う。旧接続が名簿から外れてソケットの切断を待っている間に、旧ハンドラへフレームを渡す。新接続にも、参加処理の完了後にフレームを渡す。
- 各位置で上限ちょうどと上限+1バイトの音声、start・pause・end、記録ON・OFFの7入力を確認する。観測点は `Pipeline.feed_audio`、`Lesson.control`、`Lesson.set_recording` の呼び出しと引数。処理本体は置き換える。
- 各履歴は新しい授業で同一の送信元を使う。長さ3では既定の5回の失敗による参加制限に達しない。参加制限、送信元制限、先生コードの生成・公開、他の同時実行順序、実ネットワーク、応答 JSON や切断コード、Origin、生徒数上限、不正な JSON、後段の音声処理・授業遷移・記録保存は探索の対象外。授業の自動停止と再開は [teacher-lifecycle](../teacher-lifecycle/SKILL.md) を使う。

`violation.trace` の参加順序と `probe` の入力を読む。例えば `replaced_audio` の破壊版では、先生Bの参加で先生Aを名簿から外し、Aの切断送信を保留した間にAの音声を渡すと `feed_audio` が呼ばれる。元の実装では呼ばれない。通常版の違反は、入力が実装の同じ待機点に届くことを確認してから、仕様変更・実装の退行・検証側のずれを切り分ける。タイムアウトや破壊用の置換不一致は検証の実行失敗として扱う。

## 実装との対応

同期コミットは `5381edb92062d1a35972b3a7dee8d387c5b0aa1b`。

- [server/connection.py](../../../server/connection.py) の `Connection.receive` が各受信フレームのサイズ、役割、在籍を確認して処理へ渡す。
- [server/lesson.py](../../../server/lesson.py) の `Lesson.join` が役割に対応するコードを照合し、拒否時は接続の役割を保持する。
- [server/rate_limit.py](../../../server/rate_limit.py) の `JoinRateLimiter` は探索長が制限に達しないことの照合に使う。
- [server/roster.py](../../../server/roster.py) の `Roster.add` が交代済みの先生を `_detach` してから `_close` を待つ。`Roster.contains` が現在の在籍を判定する。
- [server/main.py](../../../server/main.py) は受信したフレームを `Connection.receive` に直列に渡す。`SourceAccess` と Origin の検査を通過した接続が前提。呼出し側の照合のみ。
- [server/ws_protocol.py](../../../server/ws_protocol.py) の `parse_client_message` が JSON を参加・操作メッセージへ変換する。

対応する実装が変わったら、フレームの入力位置と期待する処理を照合する。通常版と破壊版の結果が意図どおりになった時点で、このファイルと [検証スクリプト](../../../scripts/formal/ws_teacher_ingress.py) の同期コミットを更新する。変更後の同期には `/maintain-formal-verification-skill` を使える。
