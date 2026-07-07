# LinguaBridge

先生が話す日本語の授業音声を、同一LAN上の Windows 11 機でリアルタイムに音声認識・機械翻訳し、生徒が自分の端末（Chromebook / iPhone のブラウザ）で選択した言語の字幕として閲覧できるシステム。完全ローカル動作（APIキー不要・クラウド不要・GPU不要）。

- 全体計画: [plan.md](plan.md)
- PRD・実装スライス: [GitHub Issues](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues)（PRD は #8）

## 現在の状態（イシュー #10 完了時点）

全層を貫通するトレーサー骨格が動作する。ASR・翻訳は**決定的フェイクエンジン**
（`config.yaml` の `asr.engine: fake` / `mt.engine: fake`）で、
実エンジンへの差し替えは #11（faster-whisper）・#12（hy-mt2 / NLLB）で行う。

- 先生ページ（QR・参加コード表示、マイク→16kHz PCM16 のWS送信、開始/一時停止/終了）
- サーバー（4桁コードの単一ルーム、VAD発話セグメンテーション、言語別ブロードキャスト、再接続時の履歴差分再送）
- 生徒ページ（コード入力→言語選択→字幕カード、言語の途中変更、自動スクロール）

## 開発セットアップ

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
```

## 起動

```powershell
.venv\Scripts\python -m server.main          # config.yaml を使用
.venv\Scripts\python -m server.main --config 別の設定.yaml
```

起動コンソールに参加コードと生徒用URLが表示される。

- **先生ページ**: サーバーPC上で `http://127.0.0.1:8000/teacher` を開く
  （マイクのセキュアコンテキスト要件のため localhost 必須。別端末からのHTTPS運用は #16）
- **生徒ページ**: 同一LANの端末から `http://<サーバーIP>:8000/?code=XXXX`（QRから開ける）

## テスト

```powershell
.venv\Scripts\python -m pytest       # 全テスト（WS境界＋フェイクエンジン注入）
.venv\Scripts\python -m mypy         # 型チェック
```

テスト方針は plan.md §9 を参照。機能テストは WebSocket 境界で書き、
ASR/翻訳は `ASREngine` / `TranslationEngine` 抽象に決定的フェイクを注入する。
