# LinguaBridge

先生が話す日本語の授業音声を、同一LAN上の Windows 11 機でリアルタイムに音声認識・機械翻訳し、生徒が自分の端末（Chromebook / iPhone のブラウザ）で選択した言語の字幕として閲覧できるシステム。完全ローカル動作（APIキー不要・クラウド不要・GPU不要）。

- 全体計画: [plan.md](plan.md)
- PRD・実装スライス: [GitHub Issues](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues)（PRD は #8）

## 現在の状態（イシュー #11 完了時点）

**実ASRが動作する**: マイクの日本語発話が faster-whisper（既定 whisper small int8、
`config.yaml` で kotoba-whisper-v2.0 へ切替可）で文字起こしされ、生徒カードに届く。
翻訳のみフェイク（`[en] 原文` 形式）で、実翻訳エンジンは #12 で追加する。

- 先生ページ（QR・参加コード表示、マイク→16kHz PCM16 のWS送信、開始/一時停止/終了、文字起こしライブ表示）
- サーバー（4桁コードの単一ルーム、**Silero VAD** 発話セグメンテーション（無音500ms確定・
  30s強制分割・プリロール240ms）、**幻覚フィルタ**（E-04: no_speech_prob /
  compression_ratio / avg_logprob / 既知フレーズ辞書）、起動時warmup、
  言語別ブロードキャスト、再接続時の履歴差分再送）
- 生徒ページ（コード入力→言語選択→字幕カード、言語の途中変更、自動スクロール）

実モデルが必要（下記「モデル取得」参照）。モデル・フィクスチャが無い環境では
該当する統合テストは自動スキップされる。

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

## モデル取得とベンチ（イシュー #9）

```powershell
.venv\Scripts\pip install -r requirements-bench.txt      # 実エンジン系の依存
.venv\Scripts\python scripts\download_models.py          # 全モデルを1コマンドで取得
powershell -ExecutionPolicy Bypass -File scripts\make_fixture_audio.ps1  # ベンチ用日本語音声を合成
.venv\Scripts\python scripts\bench.py                    # 実機ベンチ → docs\bench\ に報告
```

モデルは OneDrive 同期を避けるため `%LOCALAPPDATA%\LinguaBridge\models` に置かれる
（`config.yaml` の `models.dir`）。判断ゲート①の結果と既定エンジンの根拠は
[docs/bench/2026-07-07-bench.md](docs/bench/2026-07-07-bench.md) を参照。
**学校の実機（i5）へ導入する前に同コマンドで再計測すること。**

## テスト

```powershell
.venv\Scripts\python -m pytest       # 全テスト（WS境界＋フェイクエンジン注入）
.venv\Scripts\python -m mypy         # 型チェック
```

テスト方針は plan.md §9 を参照。機能テストは WebSocket 境界で書き、
ASR/翻訳は `ASREngine` / `TranslationEngine` 抽象に決定的フェイクを注入する。
