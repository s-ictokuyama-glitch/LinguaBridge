# Graph Report - LinguaBridge  (2026-08-18)

## Corpus Check
- Corpus is ~22,973 words - fits in a single context window. You may not need a graph.

## Summary
- 705 nodes · 1642 edges · 45 communities (41 shown, 4 thin omitted)
- Extraction: 88% EXTRACTED · 12% INFERRED · 0% AMBIGUOUS · INFERRED: 189 edges (avg confidence: 0.63)
- Token cost: 80,349 input · 16,970 output

## Community Hubs (Navigation)
- Acceptance & Replay Harness
- Streaming Pipeline Orchestration
- Monitoring & Reconnection Tests
- Session Recording & Audio Ingest
- ASR Engines & Hallucination Filter
- VAD Voice Segmentation
- Frame VAD & Config Schema
- Certificate & Model Download
- ASR Engine Wiring
- hy-mt2 Translation Engine
- MT Abstraction & NLLB Engine
- Student Web Client
- Benchmark Harness
- Fake ASR & Ops Tests
- Join Rate Limiting
- Real MT Integration Tests
- Teacher Web Client
- Server Entry & Model Files
- QR Code Vendor Library
- Join Code & Recording UI
- Latency Budget & ASR Benchmark
- Fake MT & Test Fixtures
- MT Engine Selection Rationale
- Overload & Silence Policy
- Network & Certificate Deployment
- Startup & Bench Workflows
- WS Protocol & Data Models
- Rate Limiter Unit Tests
- HTTP Endpoint Tests
- Audio Worklet Downsampler
- Local-Only Server Architecture
- Model Directory Config
- UI Internationalization

## God Nodes (most connected - your core abstractions)
1. `Pipeline` - 36 edges
2. `join_student()` - 36 edges
3. `AppConfig` - 32 edges
4. `join_teacher()` - 32 edges
5. `student()` - 30 edges
6. `create_app()` - 29 edges
7. `start_session()` - 26 edges
8. `send_utterance()` - 24 edges
9. `FakeASREngine` - 23 edges
10. `Session` - 22 edges

## Surprising Connections (you probably didn't know these)
- `test_supported_languages_cover_default_config()` --uses--> `AppConfig`  [INFERRED]
  tests/unit/test_mt_engines.py → server/config.py
- `app()` --uses--> `FakeASREngine`  [INFERRED]
  tests/conftest.py → server/asr/fake_engine.py
- `asr_engine()` --uses--> `FakeASREngine`  [INFERRED]
  tests/conftest.py → server/asr/fake_engine.py
- `make_app()` --uses--> `FakeASREngine`  [INFERRED]
  tests/integration/test_monitoring.py → server/asr/fake_engine.py
- `TestRealMtOverWebSocket` --uses--> `FakeASREngine`  [INFERRED]
  tests/integration/test_real_mt.py → server/asr/fake_engine.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **判断ゲート①: ベンチ計測から既定エンジン確定・設定反映までの流れ** — plan_decision_gate_1, docs_bench_2026_07_07_bench_latency_budget_judgement, docs_bench_2026_07_07_bench_decision_gate1_conclusion, config_asr_section, config_mt_section, docs_bench_2026_07_07_bench_i5_remeasure_caveat [EXTRACTED 1.00]
- **N-01 遅延バジェットを構成する処理段** — plan_n_01_latency_budget, plan_silero_vad, plan_faster_whisper_asr, plan_hy_mt2_engine, plan_nllb_engine, plan_pipeline [EXTRACTED 1.00]
- **切断からの字幕復元フロー（再接続・差分再送・順序整合）** — plan_history_resend, plan_ws_protocol, plan_session_model, plan_utterance_model, web_index_caption_screen [INFERRED 0.85]

## Communities (45 total, 4 thin omitted)

### Community 0 - "Acceptance & Replay Harness"
Cohesion: 0.07
Nodes (45): Namespace, Popen, Process, judge(), latency_stats(), LatencyStats, memory_trend(), MemoryTrend (+37 more)

### Community 1 - "Streaming Pipeline Orchestration"
Cohesion: 0.06
Nodes (39): AbstractEventLoop, ClientMessage, Exception, MTJob, Pipeline, Path, 進行中の発話を確定して処理に回す（一時停止・終了時）。, 再接続復元ジョブの成果を対象クライアントにのみ届ける。 (+31 more)

### Community 2 - "Monitoring & Reconnection Tests"
Cohesion: 0.12
Nodes (27): drain_until(), make_app(), next_stats(), 先生モニタリング（イシュー#15）のWS境界テスト。 統計の定期配信・過負荷警告・無音警告を、フェイクエンジンに gate を注入して 決定的に再現する。, 条件に合うメッセージが来るまで受信して返す。統計が定期配信されるので receive_json はブロックし続けない。, TestOverloadWarning, TestSilenceWarning, TestStatsBroadcast (+19 more)

### Community 3 - "Session Recording & Audio Ingest"
Cohesion: 0.06
Nodes (36): Any, pcm16_from_bytes(), ndarray, 先生WSから届くバイナリフレーム（16kHz mono PCM16）の受け口。, オーケストレーター（plan.md §6.3）。 teacher WS → ingest → VoiceSegmenter → asr_queue →…, Translation, Utterance, Path (+28 more)

### Community 4 - "ASR Engines & Hallucination Filter"
Cohesion: 0.07
Nodes (33): requires_fixture, requires_kotoba, requires_models, ASRResult, ABC, ndarray, ASREngine 抽象（plan.md §6.5）。テストと実装の合意済みシーム。, 発話1件分のPCM16（int16 mono）を文字起こしする。ワーカースレッドで呼ばれる。 (+25 more)

### Community 5 - "VAD Voice Segmentation"
Cohesion: 0.10
Nodes (28): EnergyVAD, ndarray, 進行中の発話を強制確定する（一時停止・終了時に呼ぶ）。, RMSエネルギーによるVAD（int16スケールの閾値）。テスト・フォールバック用。, Silero VAD（faster-whisper 同梱の ONNX モデル）のストリーミング利用。 512サンプル（32ms…, Segment, SileroVAD, VoiceSegmenter (+20 more)

### Community 6 - "Frame VAD & Config Schema"
Cohesion: 0.10
Nodes (19): field_validator, build_frame_vad(), FrameVAD, Protocol, 発話セグメンテーション。 VoiceSegmenter はフレーム単位のVAD判定から発話セグメントを組み立てる状態機械: 無音 min_silence_ms…, 設定から FrameVAD 実装を作る。フレーム長は各実装の frame_ms 属性が持つ。, HyMt2Config, Language (+11 more)

### Community 7 - "Certificate & Model Download"
Cohesion: 0.13
Nodes (20): dir_size(), download_one(), main(), ModelSpec, Path, ASR・翻訳モデルの事前ダウンロード（イシュー#9）。 1コマンドで全モデルを取得する: python scripts/download_models.py…, build_san(), generate() (+12 more)

### Community 8 - "ASR Engine Wiring"
Cohesion: 0.16
Nodes (15): ASREngine, 起動時ロード＆ダミー推論（初回遅延対策）。フェイクでは何もしない。, FasterWhisperEngine, Path, AppConfig, VadConfig, build_asr_engine(), create_app() (+7 more)

### Community 9 - "hy-mt2 Translation Engine"
Cohesion: 0.15
Nodes (16): build_mt_engine(), _require_language_coverage(), build_prompt(), HyMt2Engine, Path, Hy-MT2-1.8B（llama.cpp / GGUF int4）による TranslationEngine 実装（イシュー#12）。…, Path, 翻訳エンジンのモデル非依存部分（言語マッピング・プロンプト・エラー伝播）のユニットテスト。 実モデルでの翻訳品質・疎通は… (+8 more)

### Community 10 - "MT Abstraction & NLLB Engine"
Cohesion: 0.13
Nodes (9): ABC, TranslationEngine 抽象（plan.md §6.5）。テストと実装の合意済みシーム。, 日本語1発話を target_lang へ翻訳する。ワーカースレッドで呼ばれる。, 起動時ロード＆ダミー推論（初回遅延対策）。フェイクでは何もしない。, TranslationEngine, 決定的フェイク翻訳（#10 トレーサー用）。 日本語 → 言語マーカー付きの既知訳文: "[en] <原文>" の形式。 calls に (text_ja,…, NllbEngine, Path (+1 more)

### Community 11 - "Student Web Client"
Cohesion: 0.23
Nodes (18): addCard(), advanceWatermark(), applyFontSize(), applyI18n(), applyShowJa(), BANNER_KEYS, connect(), el (+10 more)

### Community 12 - "Benchmark Harness"
Cohesion: 0.26
Nodes (17): build_report(), load_sentences(), load_wavs(), main(), make_hymt(), make_nllb(), phase_asr(), phase_concurrent() (+9 more)

### Community 13 - "Fake ASR & Ops Tests"
Cohesion: 0.18
Nodes (10): FakeASREngine, Event, 決定的フェイクASR（#10 トレーサー用）。 既知のPCMパターン → 既知の日本語文: 発話中の最初の非ゼロサンプル値が PHRASES…, AsrConfig, MtConfig, make_app(), 運用パッケージ（イシュー#16）のサーバー側挙動テスト。 - /healthz がモデルロード完了まで 503、完了で 200（E-13） -…, TestHealthzReadiness (+2 more)

### Community 14 - "Join Rate Limiting"
Cohesion: 0.18
Nodes (7): JoinRateLimiter, 参加コード総当たり対策（plan.md E-09）。 同一IPからの連続失敗が上限に達したら一定時間 join を拒否する。…, make_ws_test_config(), WS境界テスト用の設定。VADは決定的な energy （テストが送る定数振幅PCMを Silero は音声と判定しないため）。, 再接続・堅牢化（イシュー#13）のWS境界テスト。 切断・再接続をテストクライアントで再現し、差分復元・自動一時停止/再開・…, TestJoinRateLimit, FakeClock

### Community 15 - "Real MT Integration Tests"
Cohesion: 0.15
Nodes (10): requires_hymt, requires_nllb, hymt_engine(), nllb_engine(), fixture, 実モデルを使う翻訳エンジン統合テスト（イシュー#12）。 モデル未取得の環境ではスキップされる。Hy-MT2 は temperature=0（貪欲）で…, 受け入れ基準: 設定のみで hy-mt2 が結線される（config→ファクトリの経路）。, test_build_mt_engine_constructs_hymt_from_config() (+2 more)

### Community 16 - "Teacher Web Client"
Cohesion: 0.26
Nodes (13): applyRecording(), applySessionState(), applyStats(), connect(), el, ensureMic(), handleMessage(), init() (+5 more)

### Community 17 - "Server Entry & Model Files"
Cohesion: 0.24
Nodes (10): FastAPI, _open_teacher_page_when_ready(), FastAPIエントリ。静的配信・HTTP API・WSエンドポイント。 平文HTTP(生徒用)と自己署名HTTPS(先生用)を同時リッスンする（#16）。…, モデルのロード完了（ready）を待ってから既定ブラウザで先生ページを開く。, _serve(), Path, モデルファイルの起動時検証（plan.md E-13）。 存在チェックに加えて最小サイズを検証し、ダウンロード中断などによる…, require_model_files() (+2 more)

### Community 18 - "QR Code Vendor Library"
Cohesion: 0.21
Nodes (5): a(), b(), d(), r(), s()

### Community 19 - "Join Code & Recording UI"
Cohesion: 0.18
Nodes (11): config.yaml languages セクション（en / zh 簡体字）, config.yaml recording セクション（default_on: false）, アクティブ言語のみ翻訳（言語ごとに1回）, 4桁参加コードによる単一ルーム参加, セッション記録（F-10 JSONL＋言語別Markdown・既定OFF）, 先生ページ（QR・マイク送信・モニタリング・記録トグル）, 生徒ページ 参加画面（コード入力・言語選択）, 生徒ページ 記録中インジケーター (+3 more)

### Community 20 - "Latency Budget & ASR Benchmark"
Cohesion: 0.20
Nodes (11): faster-whisper-small 計測結果（既定ASR）, kotoba-whisper-v2.0-faster 計測結果（CPU不採用）, 遅延バジェット判定表（4構成の合否）, 翻訳遅延計測（nllb vs hy-mt2）, 実教室でのASRモデル選択トレードオフ判断（small vs kotoba）, 性能受け入れ基準（45分リプレイ＋擬似生徒10接続）, 判断ゲート① 既定ASRモデル・MTエンジンの確定, N-01 遅延基準（中央値5s / 最大8s）と遅延バジェット (+3 more)

### Community 21 - "Fake MT & Test Fixtures"
Cohesion: 0.27
Nodes (7): FakeTranslationEngine, Event, app(), asr_engine(), client(), mt_engine(), fixture

### Community 22 - "MT Engine Selection Rationale"
Cohesion: 0.27
Nodes (10): config.yaml asr セクション（faster-whisper-small / int8 / ja）, config.yaml mt セクション（hy-mt2 既定 / nllb 切替）, 判断ゲート①の結論（既定 = whisper small + hy-mt2）, Tencent Hy-MT2-1.8B（Apache-2.0・公式GGUF）, faster-whisper ASR（whisper small / kotoba-whisper 切替）, hy-mt2 1.8B 翻訳エンジン（llama.cpp / GGUF int4）, NLLB-200-distilled-600M 翻訳エンジン（CTranslate2 int8）, Q-01 hy-mt2 1.8b の配布元・GGUF・ライセンス（解消済み） (+2 more)

### Community 23 - "Overload & Silence Policy"
Cohesion: 0.27
Nodes (10): config.yaml monitoring セクション（統計間隔・無音警告・過負荷閾値）, config.yaml vad セクション（silero / 500ms / 30s / 240ms）, ASREngine 抽象インターフェース, E-04 Whisper幻覚フィルタ, E-03 max_utterance_s による30秒強制分割, E-05 過負荷制御（発話を落とさずキュー深度警告）, pipeline.py サーバー内部パイプライン, Silero VAD 発話セグメンテーション (+2 more)

### Community 24 - "Network & Certificate Deployment"
Cohesion: 0.20
Nodes (10): config.yaml server セクション（8000/8443・証明書）, Phase A 校内Wi-Fi AP分離確認（判断ゲート②）, Phase B 管理Chromebookの証明書・マイク可否（判断ゲート③）, Phase C 教室リハーサル, 判断ゲート② 管理Chromebookでの自己署名HTTPS承認可否, HTTPS(8443)/HTTP(8000) 二重リッスン, Windowsモバイルホットスポット副構成（AP分離対策）, テスト戦略（WS境界＋フェイクエンジン注入） (+2 more)

### Community 25 - "Startup & Bench Workflows"
Cohesion: 0.29
Nodes (7): 開発機計測の限界と学校実機(i5)再計測の必要性, 事前準備（当日ネット不要化のための前日までのモデル取得・計測）, N-09 start.bat ダブルクリックだけで完結する運用, 性能受け入れ試験ワークフロー（#17 replay_client）, モデル取得とベンチのワークフロー（#9）, start.bat 起動フロー（初回自動セットアップ・再開可能）, ベンチ・モデル取得依存（huggingface_hub / psutil / websockets）

### Community 26 - "WS Protocol & Data Models"
Cohesion: 0.33
Nodes (7): F-11 履歴差分再送による再接続復元（last_seq）, Session / Client データモデル（単一ルーム）, Utterance / Translation データモデル, WebSocketプロトコル（/ws JSON＋PCMバイナリ）, 生徒ページ（参加→言語選択→字幕カード）, 生徒ページ 字幕画面（カード・原文併記・文字サイズ・最新へ）, 先生ページ 配信制御（開始/一時停止/終了・マイクメーター）

### Community 27 - "Rate Limiter Unit Tests"
Cohesion: 0.48
Nodes (6): make(), 参加コード総当たり対策（E-09）のユニットテスト。時計を注入して待たずに検証する。, test_blocked_at_threshold(), test_not_blocked_below_threshold(), test_success_resets_failure_count(), test_unblocked_after_block_period()

### Community 30 - "Local-Only Server Architecture"
Cohesion: 0.67
Nodes (3): 完全ローカル動作（APIキー不要・クラウド不要・GPU不要）, LinguaBridge 授業リアルタイム翻訳システム, フルサーバー集中型アーキテクチャ

## Ambiguous Edges - Review These
- `判断ゲート② 管理Chromebookでの自己署名HTTPS承認可否` → `Phase B 管理Chromebookの証明書・マイク可否（判断ゲート③）`  [AMBIGUOUS]
  docs/field-verification-checklist.md · relation: implements

## Knowledge Gaps
- **23 isolated node(s):** `I18N`, `state`, `el`, `BANNER_KEYS`, `state` (+18 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `判断ゲート② 管理Chromebookでの自己署名HTTPS承認可否` and `Phase B 管理Chromebookの証明書・マイク可否（判断ゲート③）`?**
  _Edge tagged AMBIGUOUS (relation: implements) - confidence is low._
- **Why does `Pipeline` connect `Streaming Pipeline Orchestration` to `ASR Engine Wiring`, `Server Entry & Model Files`, `Session Recording & Audio Ingest`, `VAD Voice Segmentation`?**
  _High betweenness centrality (0.081) - this node is a cross-community bridge._
- **Why does `AppConfig` connect `ASR Engine Wiring` to `Streaming Pipeline Orchestration`, `Session Recording & Audio Ingest`, `ASR Engines & Hallucination Filter`, `Frame VAD & Config Schema`, `Certificate & Model Download`, `hy-mt2 Translation Engine`, `Fake ASR & Ops Tests`, `Join Rate Limiting`, `Real MT Integration Tests`, `Server Entry & Model Files`, `Fake MT & Test Fixtures`?**
  _High betweenness centrality (0.048) - this node is a cross-community bridge._
- **Why does `load_config()` connect `Certificate & Model Download` to `Acceptance & Replay Harness`, `Frame VAD & Config Schema`, `ASR Engine Wiring`, `Benchmark Harness`, `Server Entry & Model Files`?**
  _High betweenness centrality (0.044) - this node is a cross-community bridge._
- **Are the 9 inferred relationships involving `Pipeline` (e.g. with `create_app()` and `_open_teacher_page_when_ready()`) actually correct?**
  _`Pipeline` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Are the 16 inferred relationships involving `AppConfig` (e.g. with `build_asr_engine()` and `build_mt_engine()`) actually correct?**
  _`AppConfig` has 16 INFERRED edges - model-reasoned connections that need verification._
- **Are the 28 inferred relationships involving `student()` (e.g. with `.test_overload_appears_on_backlog_and_clears_when_drained()` and `.test_mic_silent_warning_after_live_silence()`) actually correct?**
  _`student()` has 28 INFERRED edges - model-reasoned connections that need verification._