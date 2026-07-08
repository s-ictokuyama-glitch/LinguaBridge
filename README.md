# LinguaBridge

先生が話す日本語の授業音声を、同一LAN上の Windows 11 機でリアルタイムに音声認識・機械翻訳し、生徒が自分の端末（Chromebook / iPhone のブラウザ）で選択した言語の字幕として閲覧できるシステム。完全ローカル動作（APIキー不要・クラウド不要・GPU不要）。

- 全体計画: [plan.md](plan.md)
- PRD・実装スライス: [GitHub Issues](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues)（PRD は #8）

## 現在の状態（イシュー #16 完了時点）

**ASR・翻訳とも実エンジンで動作する**: マイクの日本語発話が faster-whisper
（既定 whisper small int8、kotoba へ切替可）で文字起こしされ、
**Hy-MT2-1.8B**（既定・GGUF/llama.cpp）または **NLLB-200 600M**（CTranslate2、
`mt.engine: nllb` で切替）で英語・中国語（簡体字）へ実翻訳されて生徒カードに届く。
実測遅延は発話終了→表示で約1.7〜2.0秒（開発機、docs/bench 参照）。

- 先生ページ（QR・参加コード表示、マイク→16kHz PCM16 のWS送信、開始/一時停止/終了、
  文字起こしライブ表示、**モニタリング**（2秒ごとの統計＝接続数・言語内訳・キュー深度・
  遅延中央値、マイク入力レベルメーター、30秒無音警告、キュー滞留の過負荷警告））
- サーバー（4桁コードの単一ルーム、**Silero VAD** 発話セグメンテーション（無音500ms確定・
  30s強制分割・プリロール240ms）、**幻覚フィルタ**（E-04: no_speech_prob /
  compression_ratio / avg_logprob / 既知フレーズ辞書）、起動時warmup、
  **アクティブ言語のみ翻訳・言語ごとに1回だけ翻訳**、言語別ブロードキャスト、
  再接続時の履歴差分再送＋**訳文が無い分のオンデマンド翻訳**、
  先生切断時の自動一時停止と再接続での自動再開、先生二重接続の後勝ち、
  参加コード総当たり対策（同一IP 5連続失敗で60秒拒否））
- 生徒ページ（コード入力→言語選択→字幕カード、言語の途中変更、原文併記トグル・
  文字サイズ3段階（再読み込み後も保持）・追従一時停止＋「最新へ」・UI多言語化
  （ja/en/zh）・遅延中タグ・状態バナー、
  **指数バックオフの自動再接続**と切断バナー、seq順挿入・重複排除）

実モデルが必要（下記「モデル取得」参照）。モデル・フィクスチャが無い環境では
該当する統合テストは自動スキップされる。

---

# 先生向け運用手順書

ITに詳しくなくても、**初回に setup.ps1 を1回**、**毎授業は start.bat をダブルクリック**するだけで運用できます。

## 初回セットアップ（1回だけ・管理者権限）

1. このフォルダの `setup.ps1` を右クリック →「PowerShell で実行」
   （または管理者PowerShellで `powershell -ExecutionPolicy Bypass -File setup.ps1`）
2. 自動で次を行います（モデルDLに数十分かかることがあります）:
   - Python仮想環境の作成と依存インストール
   - AI モデルのダウンロード（`%LOCALAPPDATA%\LinguaBridge\models` へ。約4GB）
   - 先生ページ用の自己署名HTTPS証明書の生成
   - ファイアウォールで受信ポート（8000/8443）を許可
   - AC電源接続中のスリープ・休止の無効化

> 管理者権限がない場合、モデルDLと証明書生成までは動きますが、ファイアウォール許可と
> スリープ無効化は手動になります（[トラブルシューティング](#トラブルシューティング)参照）。

## 毎授業の起動

1. `start.bat` を**ダブルクリック**
2. 黒いウィンドウに **参加コード**と**生徒用URL**が表示されます（授業中は閉じない）
3. モデルのロードが終わると、**先生ページが自動で開きます**
   - 初回だけブラウザに証明書の警告が出ます →「詳細設定」→「（安全でない）サイトへ進む」で承認
   - マイクの許可を求められたら「許可」
4. 「開始」ボタンで配信開始。話すと生徒の端末に字幕が出ます

## 生徒の参加

- 先生ページに大きく表示される **QRコード**を読む、または **生徒用URL＋4桁コード**を入力
- 言語（英語／中国語簡体字）を選ぶと字幕が始まります
- 生徒ページは**警告なしのHTTP**で開けます（証明書の承認は不要）

## ネットワーク構成（2通り）

| 構成 | 使う場面 | 手順 |
|------|----------|------|
| **校内Wi-Fi直結**（主） | 校内Wi-Fiで端末同士が通信できる場合 | サーバーPCと生徒端末を同じWi-Fiに接続するだけ |
| **Windowsモバイルホットスポット**（副） | 校内WiFiが端末間通信を遮断（AP分離）する場合 | 下記 |

**ホットスポット構成の切替**:
1. Windows設定 →「ネットワークとインターネット」→「モバイル ホットスポット」をオン
2. サーバーPC自身がホットスポットの主なので、生徒端末をそのホットスポットに接続
3. `start.bat` を起動し直す（新しいIPでQRが再生成されます）

> ホットスポットは**同時接続8台**まで。10人学級では「2人で1台を見る」ペア閲覧や、
> 先生機自身は配信に使わない運用でスロットを節約してください。どうしても足りない場合は
> 安価なトラベルルーターの追加を学校に相談（無料要件の例外として要合意）。

## トラブルシューティング

- **マイクが使えない/許可が出ない**: 先生ページはHTTPS（`https://…:8443/teacher`）で開いていますか。
  学校のChromebook等でHTTPS警告を承認できない場合は、サーバーPC上で
  `http://127.0.0.1:8000/teacher`（localhost）を開けばマイクが使えます。
- **生徒がつながらない**: 校内Wi-FiのAP分離が原因のことが多いです。上記ホットスポット構成へ切替。
- **「処理が追いついていません」の警告**: 早口・長文が続くと出ます。少しゆっくり、区切って話してください。
- **30秒無音の警告**: マイクがミュート/未接続の可能性。先生ページの入力レベルメーターを確認。
- **授業中にPCがスリープ**: setup.ps1 実行済みならAC接続中は無効化されています。必ずAC電源に接続を。
- **翻訳ライセンス**: 既定の翻訳エンジン Hy-MT2 は Apache-2.0 で制約なし。`mt.engine: nllb` に切り替えた
  場合、NLLB-200 は **CC-BY-NC 4.0（非商用限定）** です。学校の授業利用は非商用の想定ですが、
  商用利用に転用しないでください。

---

# 開発者向け

## 開発セットアップ

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
```

## 起動（手動）

```powershell
.venv\Scripts\python -m server.main                 # config.yaml を使用
.venv\Scripts\python -m server.main --open-browser  # 起動後に先生ページを開く（start.bat 相当）
.venv\Scripts\python -m server.main --config 別の設定.yaml
```

証明書（`certs/cert.pem`, `certs/key.pem`）があればHTTP(8000)とHTTPS(8443)を同時リッスンし、
無ければHTTP単独で起動する（先生はサーバーPCの `http://127.0.0.1:8000/teacher` を使う）。
証明書は `python scripts\make_cert.py` で生成。死活確認 `/healthz` はモデルロード完了まで503。

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
