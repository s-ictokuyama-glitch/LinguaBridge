---
name: ws-teacher-ingress
description: 現在の先生だけが WebSocket から音声・授業操作・記録切替を入力できることを実コードの有界探索で確認する。Use when changing create_app.ws_endpoint dispatch, its audio byte limit, Lesson.join role changes, or Roster.add detachment during teacher replacement.
---

# 現在の先生だけに WebSocket 入力を許可する

`/ws` は、現在の先生の上限内の音声、授業操作、記録切替だけを対応する処理へ一度ずつ渡す。違反すると、生徒や交代済みの先生の入力で字幕、授業の進行、記録が変わる。

## 実行する

リポジトリ直下で実行する。実際の FastAPI アプリに ASGI フレームを渡すので、ブラウザーやモデルの起動は不要。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt --with fastapi==0.141.1 --with starlette==1.7.0 --with psutil==7.2.2 python -X utf8 -B scripts/formal/ws_teacher_ingress.py --mutation-check
```

同じ版の依存がある環境では `.venv/Scripts/python.exe -X utf8 -B scripts/formal/ws_teacher_ingress.py --mutation-check` も使える。Windows の制限環境で asyncio が起動時に停止する場合は、内部 socketpair の実行権限を確認する。

終了コード 0、`status: PASS`、`complete_scenarios: 86`、6 件の破壊すべての `detected: true` を確認する。破壊版は一時コピーの `main.py` を使う。

## 結果を読む

- 参加履歴は、先生・生徒と正しい・誤った参加コードの4通りの `join` を長さ0〜3で全列挙する。受理した参加だけが役割を更新する。85履歴を調べる。
- 先生交代は2接続で1回行う。旧接続が名簿から外れて切断の ASGI 送信を待っている間に、旧ハンドラへフレームを渡す。新接続にも、参加処理の完了後にフレームを渡す。
- 各位置で上限ちょうどと上限+1バイトの音声、start・pause・end、記録ON・OFFの7入力を確認する。観測点は `Pipeline.feed_audio`、`Lesson.control`、`Lesson.set_recording` の呼び出しと引数。処理本体は置き換える。
- 対象外は他の同時実行順序、実ネットワーク、応答 JSON や切断コード、Origin、参加のレート制限、生徒数上限、不正な JSON、後段の音声処理・授業遷移・記録保存。授業の自動停止と再開は [teacher-lifecycle](../teacher-lifecycle/SKILL.md) を使う。

`violation.trace` の参加順序と `probe` の入力を読む。例えば `replaced_audio` の破壊版では、先生Bの参加で先生Aを名簿から外し、Aの切断送信を保留した間にAの音声を渡すと `feed_audio` が呼ばれる。元の実装では呼ばれない。通常版の違反は、入力が実装の同じ待機点に届くことを確認してから、仕様変更・実装の退行・検証側のずれを切り分ける。タイムアウトや破壊用の置換不一致は検証の実行失敗として扱う。

## 実装との対応

同期コミットは `0194bc1b0973cc6ab1fdb16def022d78f2e6a9fa`。

- [server/main.py](../../../server/main.py) の `create_app.ws_endpoint` が各受信フレームのサイズ、役割、在籍を確認して処理へ渡す。
- [server/lesson.py](../../../server/lesson.py) の `Lesson.join` が参加コードを検査し、拒否時は接続の役割を保持する。
- [server/roster.py](../../../server/roster.py) の `Roster.add` が交代済みの先生を `_detach` してから `_close` を待つ。`Roster.contains` が現在の在籍を判定する。
- [server/ws_protocol.py](../../../server/ws_protocol.py) の `parse_client_message` が JSON を参加・操作メッセージへ変換する。

対応する実装が変わったら、フレームの入力位置と期待する処理を照合する。通常版と破壊版の結果が意図どおりになった時点で、このファイルと [検証スクリプト](../../../scripts/formal/ws_teacher_ingress.py) の同期コミットを更新する。変更後の同期には `/maintain-formal-verification-skill` を使える。
