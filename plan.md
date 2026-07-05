# LinguaBridge — 授業リアルタイム翻訳字幕システム 実装計画

先生が Chromebook で話す日本語を文字起こしし、生徒が各自のスマホで選んだ言語（英・中・ポルトガル語・ベトナム語など）の字幕として表示する Web アプリの詳細計画。

## 確定済みの前提（ヒアリング・調査結果）

| # | 論点 | 決定 |
|---|------|------|
| 1 | 端末 | 先生 = GIGA 級 Chromebook（Celeron N4000系/MediaTek・RAM 4GB）。生徒 = iPhone/Android スマホ |
| 2 | 回線 | 生徒スマホも学校 Wi-Fi（同一 LAN）。ただし AP アイソレーション前提でインターネット経由中継 |
| 3 | 要件の本質 | オンデバイスは手段。無料・APIキー不要が本質要件（音声の Google サーバー処理は許容） |
| 4 | ASR | 先生端末 Chrome の Web Speech API（ja-JP・無料・低負荷）。Whisper は先生端末スペック的に不採用 |
| 5 | 対応言語 | 英・中（簡体字）＋ポルトガル語・ベトナム語など数言語。言語リストは設定で拡張可能に |
| 6 | 翻訳場所 | 生徒端末オンデバイス（transformers.js）。※Chrome 内蔵 Translator API は ChromeOS・モバイル非対応（[公式ドキュメント](https://developer.chrome.com/docs/ai/get-started)）のため棄却 |
| 7 | 非対応端末 | WebGPU 対応スマホを参加条件にする。緩和策として「日本語原文のみモード」を用意 |
| 8 | 翻訳モデル | NLLB-200-distilled-600M（q4・約350MB）で開始。[HY-MT1.5-1.8B](https://huggingface.co/tencent/HY-MT1.5-1.8B)（≈1.1GB・高品質・33言語）はスパイク検証後にオプション化 |
| 9 | 中継 | GAS（Google Apps Script）優先＝HTTP ポーリング方式（GAS は WebSocket 不可）。スパイクで基準未達なら Cloudflare Workers + Durable Objects（WebSocket）に差し替え。通信層は抽象化して両対応 |
| 10 | 規模 | 当面は少人数パイロット（数名〜1クラス40人以下・同時1教室） |

## 1. Project Overview / Goals

- 先生の日本語発話を文字起こしし、生徒が各自選んだ言語の字幕としてスマホに表示する Web アプリ「LinguaBridge」。日本語が不得手な生徒が授業内容を理解し、授業についていけるようにする。
- ゴール:
  - (a) 発話から字幕表示まで p90 ≤ 7秒
  - (b) 完全無料運用・APIキー不要
  - (c) 生徒は QR コード読み取りだけで参加
  - (d) 授業1コマ（50分）連続稼働
- 非ゴール（初期リリース）: 音声読み上げ（TTS）、複数教室同時運用、非対応スマホへのサーバー翻訳提供、翻訳履歴の永続保存。

## 2. Requirements

### Functional

- F1: 先生端末（Chromebook・Chrome）でマイクから日本語を連続音声認識し、確定文単位で配信する。
- F2: 先生画面にルームコードと QR コードを表示。生徒は QR 読み取り→言語選択のみで参加。
- F3: 生徒端末で en / zh-Hans / pt / vi から表示言語を選択（設定ファイルで言語追加可能）。
- F4: 生徒端末上で翻訳を実行し字幕表示（オンデバイス、transformers.js + NLLB-600M）。
- F5: 日本語原文のみモード（翻訳非対応端末・原文を読みたい生徒向け）。
- F6: 再接続・途中参加時に直近の字幕履歴を復元（直近200文）。
- F7: 字幕のフォントサイズ変更、自動スクロール（タップで一時停止）。

### Non-functional

- N1: 費用ゼロ（GitHub Pages + GAS 無料枠 or Cloudflare 無料枠）。APIキー・アカウント登録を利用者（先生・生徒）に要求しない。
- N2: 遅延: 発話終了→生徒画面表示 p90 ≤ 7秒（内訳目安: ASR確定 ~1s + 中継ポーリング ≤3s + 翻訳 0.5〜3s）。
- N3: 先生端末は RAM 4GB・低速 CPU で安定動作（重い処理を先生端末に置かない）。
- N4: 生徒端末要件: WebGPU 対応ブラウザ（Android 12+ Chrome / iOS 26+ Safari 目安）＋初回モデルDL約350MB（校内 Wi-Fi・2回目以降はキャッシュ）。
- N5: プライバシー: 音声は Web Speech API 経由で Google 処理、文字起こしテキストは中継サーバー（GAS = Google インフラ）経由。個人名等が含まれ得ることを運用ドキュメントに明記し、履歴はセッション終了で破棄。

## 3. Architecture / Tech Stack Decisions

```
先生 Chromebook (Chrome)                     生徒スマホ ×N
┌────────────────────────┐                  ┌────────────────────────┐
│ teacher.html            │                  │ student.html            │
│ Web Speech API (ja-JP)  │                  │ 言語選択 en/zh/pt/vi     │
│ 確定文 → publish        │                  │ poll → ja確定文受信      │
└──────────┬─────────────┘                  │ Web Worker:              │
           │ POST (text/plain)               │  transformers.js         │
           ▼                                 │  NLLB-600M q4 (WebGPU)   │
┌────────────────────────┐   GET (2〜3s間隔) │ → 字幕表示               │
│ 中継: GAS Webアプリ      │◄─────────────────┴────────────────────────┘
│ CacheServiceに文リング   │
│ バッファ (seq付き)       │   ※スパイク不合格なら Cloudflare Workers+DO
└────────────────────────┘     (WebSocket) に差し替え。通信層は抽象化。
```

- **フロント**: Vite + TypeScript、UI フレームワークなし（2画面の小規模アプリ。依存最小・GIGA 端末でも軽量）。GitHub Pages（このリポジトリ）で静的ホスティング、GitHub Actions で自動デプロイ。
- **ASR**: Web Speech API（`webkitSpeechRecognition`、continuous + interimResults）。無料・キー不要・低負荷で、低スペック先生端末でも実時間動作。Whisper（WebGPU）は N4000 + 4GB では実時間性・精度とも不足のため不採用。
- **翻訳**: transformers.js v3 + `Xenova/nllb-200-distilled-600M`（WebGPU・q4）。Web Worker 内で実行し UI をブロックしない。HY-MT1.5-1.8B はスパイク（S4）で実行可否を検証し、可なら高スペック端末向け選択肢として追加。
- **中継**: GAS Web アプリ（第一候補）。理由: 学校フィルタで script.google.com はまず確実に通る・無料・運用者が Google アカウントだけで持てる。制約: WebSocket 不可→ポーリング（+1〜3s遅延）、同時実行30・日次クォータ→パイロット規模なら許容見込み（S2 で実測）。不合格時は Cloudflare Workers + Durable Objects（WebSocket・常時即応）。
- **QR コード**: `qrcode` npm パッケージでクライアント生成。

## 4. File / Directory Structure

```
LinguaBridge/
├── plan.md
├── README.md                     # セットアップ・授業前チェックリスト
├── web/                          # 静的アプリ (GitHub Pages)
│   ├── index.html                # ランディング（先生/生徒の入口）
│   ├── teacher.html
│   ├── student.html
│   ├── vite.config.ts
│   └── src/
│       ├── shared/
│       │   ├── types.ts          # Segment, PollResponse 等
│       │   ├── config.ts         # 言語リスト, 中継URL, モデルID
│       │   └── transport/
│       │       ├── RelayTransport.ts      # インターフェース
│       │       ├── GasPollingTransport.ts
│       │       └── CloudflareWsTransport.ts  # (必要時)
│       ├── teacher/
│       │   ├── main.ts
│       │   ├── SpeechRecognizer.ts   # 再起動ループ・ウォッチドッグ内蔵
│       │   └── ui.ts                 # 部屋作成/QR/ライブ原文表示
│       └── student/
│           ├── main.ts
│           ├── capability.ts         # WebGPU/メモリ判定
│           ├── translator.worker.ts  # transformers.js 実行
│           ├── TranslatorClient.ts   # worker橋渡し・バックログ統合
│           └── ui.ts                 # 字幕フィード/言語選択/フォント
├── relay-gas/
│   ├── Code.gs                   # doGet/doPost, CacheService, LockService
│   └── appsscript.json
├── relay-cf/                     # GAS不合格時のみ作成
│   ├── wrangler.toml
│   └── src/index.ts              # Worker + Durable Object (Room)
└── tools/
    └── load-test.mjs             # 40クライアント模擬ポーリング
```

## 5. Data Models / Schema

```ts
// 確定文（中継を流れる唯一のデータ）
type Segment = {
  seq: number;      // ルーム内単調増加。重複排除・欠落検出キー
  text: string;     // 日本語確定文
  tMs: number;      // 先生端末での確定時刻 (epoch ms)。遅延計測用
};

type CreateRoomResponse = { roomId: string; teacherToken: string };
type PollResponse = { segments: Segment[]; latestSeq: number; active: boolean };

// GAS CacheService (TTL 6h)
// key `room:{id}:meta` = { teacherTokenHash, createdAt, latestSeq }
// key `room:{id}:log`  = Segment[] リングバッファ（直近200文, ~50KB上限）

// 生徒端末 localStorage
type StudentPrefs = { lang: 'en'|'zh-Hans'|'pt'|'vi'|'ja-raw'; fontScale: number };
```

NLLB 言語コード対応: ja=`jpn_Jpan` → en=`eng_Latn` / zh-Hans=`zho_Hans` / pt=`por_Latn` / vi=`vie_Latn`。

## 6. API / Component Design

### 中継 API（GAS Web アプリ・全て匿名アクセス可でデプロイ）

| 操作 | リクエスト | レスポンス | 備考 |
|---|---|---|---|
| ルーム作成 | POST `?action=create`（body: text/plain JSON） | `CreateRoomResponse` | roomId は6桁英数字 |
| 文の配信 | POST `?action=publish`（body: {roomId, teacherToken, segments[]}） | `{ok, latestSeq}` | LockService で追記を直列化 |
| ポーリング | GET `?action=poll&roomId=X&sinceSeq=N` | `PollResponse` | sinceSeq より後の文のみ返す |
| 終了 | POST `?action=close` | `{ok}` | active=false に |

CORS 対策（GAS 既知の制約への対応）: POST は `Content-Type: text/plain` でプリフライト回避、GET はカスタムヘッダーなしの単純リクエストのみ。302 リダイレクト（script.googleusercontent.com）は fetch の自動追従に任せる。この挙動一式はスパイク S2 の検証項目。

### `RelayTransport` インターフェース（GAS/Cloudflare 差し替えの要）

```ts
interface RelayTransport {
  createRoom(): Promise<CreateRoomResponse>;                 // 先生
  publish(segments: Segment[]): Promise<void>;               // 先生
  join(roomId: string, sinceSeq: number): void;              // 生徒
  onSegments(cb: (batch: Segment[]) => void): void;          // 生徒
  onStatus(cb: (s: 'connected'|'reconnecting'|'closed') => void): void;
  close(): void;
}
```

GasPollingTransport: 2〜3秒間隔（±ジッター）でポーリング、失敗時指数バックオフ、`visibilitychange` で復帰時即時キャッチアップ。

### SpeechRecognizer（先生）

continuous 認識、`onend`/`onerror` で自動再起動、15秒無結果ウォッチドッグ→UI 警告、interim はローカル表示のみ・確定文だけ publish。未送信文はキューに保持しネットワーク断でも再送（seq で冪等）。

### TranslatorClient / translator.worker（生徒）

pipeline を言語選択時に一度だけ生成（進捗バー付き DL → Cache API に自動キャッシュ）。受信文を FIFO で翻訳。**バックログ制御**: 待ち行列が3文を超えたら複数文を連結して1回で翻訳（捨てない・追いつく）。原文は受信直後にグレー表示し、訳文完成時に置換（体感遅延の緩和）。NLLB の反復生成対策に `max_new_tokens` 上限と `no_repeat_ngram_size` を設定。

### 画面

- 先生: 開始/停止・マイクレベル・ライブ原文・QR/ルームコード・送信状況。
- 生徒: 言語選択→字幕フィード（自動スクロール・タップで停止）・フォント A±・接続状態バナー・Wake Lock（画面消灯防止）。

## 7. Step-by-step Implementation Plan

1. **S1 スパイク: Web Speech API 実機検証** — 先生用 GIGA Chromebook 実機で ja 連続認識の精度・自動停止頻度・再起動の隙間を計測 → go/no-go。
2. **S2 スパイク: GAS 中継検証** — create/publish/poll 最小実装 → CORS 挙動、往復遅延、`tools/load-test.mjs` で40クライアント負荷、クォータ消費を実測 → **GAS 採用 or Cloudflare 切替を決定**。
3. **S3 スパイク: 生徒スマホで NLLB-600M 検証** — パイロット参加予定の実機で DL 時間・1文あたり翻訳時間・メモリ・発熱を計測。iOS 26 Safari + transformers.js WebGPU の互換性確認 → モデル/dtype 確定。
4. リポジトリ足場: Vite + TS ワークスペース、GitHub Actions → GitHub Pages デプロイ、`shared/types.ts`・`config.ts`。
5. `RelayTransport` インターフェース + `GasPollingTransport` 実装（S2 の成果物を本実装化）＋ユニットテスト（seq 重複排除・バックオフ）。
6. 先生アプリ MVP: SpeechRecognizer + ルーム作成 + QR 表示 + publish。実機で30分連続稼働確認。
7. 生徒アプリ MVP（翻訳なし）: QR 参加 → 日本語原文フィード表示（= F5 の原文モードがこの時点で完成）。
8. 翻訳統合: capability 判定 → worker で NLLB ロード → 言語選択 → 字幕置換表示、バックログ統合ロジック＋ユニットテスト。
9. UX 仕上げ: フォント調整、Wake Lock、再接続バナー、モデル DL 進捗、途中参加の履歴復元。
10. 教室パイロット（生徒数名・1コマ）: 遅延ログ（tMs→表示時刻）収集、翻訳品質の聞き取り調査。
11. **S4 スパイク（任意）: HY-MT1.5-1.8B** のブラウザ実行検証（ONNX/WebLLM 経路調査・8GB 級 Android 実機）→ 可なら高スペック端末向けモデル選択 UI を追加。
12. パイロット結果を README（運用手順・授業前チェックリスト）に反映。必要なら Cloudflare 移行・PWA 化・複数クラス対応を次期スコープとして起票。

## 8. Edge Cases & Error Handling

- **Web Speech の無通告停止**（無音・約60秒で切れる既知挙動）→ onend 即再起動＋ウォッチドッグ。再起動の隙間で数語落ちるのは仕様として README に明記。
- **ネットワーク断**: 先生側は未送信キュー保持→復帰時一括再送（seq 冪等）。生徒側は指数バックオフ＋「再接続中」バナー、復帰時 sinceSeq でキャッチアップ。
- **スマホのバックグラウンド化/画面ロック**（iOS はタイマー停止）→ Wake Lock + `visibilitychange` 復帰時に即ポーリング。
- **翻訳バックログ**: 連結翻訳で追いつく（文は捨てない）。連結時は表示も段落として結合。
- **短い相槌・空文字**（「はい」「えー」）: 3文字未満はスキップ or 直後の文に結合（翻訳無駄打ち防止）。
- **GAS 同時実行超過（429相当）**: ポーリング間隔にジッター、エラー時バックオフ。
- **リングバッファ溢れ**（200文超の途中参加）: 「これ以前の履歴はありません」表示。
- **非対応端末**: capability 判定で明確なメッセージ＋日本語原文のみモードへ誘導。
- **iOS の7日ストレージ削除**: モデル再 DL になる旨と「ホーム画面に追加」推奨を README に記載。
- **誤認識・専門用語**: 先生画面に原文が見えるので言い直しで対処（用語集機能は次期スコープ。NLLB は用語固定非対応という制約も明記）。

## 9. Testing Strategy

- **ユニット（Vitest）**: seq 重複排除・欠落検出、バックログ統合、SpeechRecognizer の再起動ステートマシン（認識 API はモック）、transport のレスポンス解析。
- **統合**: ステージング GAS デプロイに対する fetch テスト（create→publish→poll の一連）、`tools/load-test.mjs` による40クライアント負荷。
- **実機マトリクス（手動・チェックリスト化）**: 先生 GIGA Chromebook / Android 中位・低位 / iPhone（iOS 26）× 各言語。30分連続稼働。
- **遅延計測**: Segment.tMs と生徒表示時刻の差分を画面内デバッグ表示 → p50/p90 記録。合格基準 p90 ≤ 7s。
- **翻訳品質**: 授業想定の日本語20文セットを en/zh/pt/vi へ翻訳し、可能な範囲でネイティブ/教員チェック。HY-MT 検証時の比較基準にも使う。

## 10. Potential Risks & Mitigations

| リスク | 影響 | 緩和策 |
|---|---|---|
| GIGA 端末で Web Speech の精度・安定性不足 | 根幹 | S1 を最初に実施。外付けマイク検討。最悪 Whisper tiny 実験（期待薄）または先生端末のみ別機種 |
| GAS のポーリング遅延・クォータ | 中 | S2 で実測。不合格なら Cloudflare へ（transport 抽象化済みなので差し替えのみ） |
| iOS 26 Safari + transformers.js WebGPU の互換性が未成熟 | 大 | S3 でパイロット参加者の実機を最優先検証。動かない iPhone は原文モード or 参加条件の再周知 |
| NLLB-600M の ja→vi / ja→pt 品質不足 | 中 | 品質評価セットで事前確認。HY-MT スパイク（S4）を前倒し。字幕は「補助手段」と位置付けて期待値調整 |
| 学校フィルタが huggingface.co（モデル CDN）や github.io をブロック | 大 | 授業前チェックリストに疎通確認を入れる。ブロック時はモデルを GitHub Releases（2GB/file 可）から配布する代替ホスティングに切替 |
| プライバシー懸念（音声→Google、テキスト→GAS） | 中 | 学校が Google Workspace 利用なら整合的である旨を説明資料化。履歴非保存・匿名参加を設計で担保 |
| 無料枠の仕様変更 | 小 | transport 抽象化と静的ホスティング中心の構成で乗り換え容易 |

## 11. Assumptions & Open Questions

### Assumptions

- 先生端末で Chrome 利用可・マイク許可可能。
- 生徒スマホは WebGPU 対応（参加条件）。
- 同時1教室・40人以下。
- 中国語は簡体字先行。
- 字幕履歴の永続保存は不要。
- 開発者の無料 Google アカウントで GAS をデプロイできる。

### Open Questions（実装前〜パイロットで解消）

1. パイロット参加生徒の実際のスマホ機種（S3 の検証対象を決める）
2. 学校フィルタで huggingface.co / github.io / script.google.com が全端末から到達可能か
3. 繁体字（zh-Hant）の需要有無
4. 教科固有用語の誤訳がどの程度実害になるか（用語集機能の優先度判断）
