# LinguaBridge — 授業リアルタイム字幕・翻訳

先生が Chromebook で話す日本語を文字起こしし、生徒が各自のスマホで選んだ言語の字幕として表示するシステム。詳細設計は [plan.md](./plan.md)、要件は [Issue #1 (PRD)](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/1) を参照。

**現在の実装状況: スライス1（[#2](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/2)）**
— 先生画面の手入力テキストが GAS 中継経由で生徒画面に届く「歩くスケルトン」。音声認識（#3）・オンデバイス翻訳（#4）は今後のスライスで追加。

## 構成

```
web/          先生・生徒画面（Vite + TypeScript、GitHub Pages 配信）
relay-gas/    中継サーバー（Google Apps Script Web アプリ）
```

- 先生画面 (`teacher.html`): ルーム作成 → ルームコード + QR 表示 → 文を配信
- 生徒画面 (`student.html`): QR/ルームコードで参加 → 字幕フィード表示（2〜3秒間隔のポーリング）
- 通信層は `RelayTransport` インターフェースに抽象化されており、GAS が制約に当たった場合は Cloudflare Workers 実装に差し替え可能

## セットアップ

### 1. GAS 中継のデプロイ（初回のみ・約5分）

1. [script.google.com](https://script.google.com) で「新しいプロジェクト」を作成
2. `relay-gas/Code.gs` の内容をエディタに貼り付けて保存
3. 「デプロイ」→「新しいデプロイ」→ 種類「ウェブアプリ」
   - 実行ユーザー: **自分**
   - アクセスできるユーザー: **全員**
4. 発行された `https://script.google.com/macros/s/…/exec` URL をコピー
5. `web/src/shared/config.ts` の `DEFAULT_RELAY_URL` に貼り付けてコミット

> 開発中は URL パラメータ `?relay=<execのURL>` でも上書きできます（localStorage に保持され、同じ端末のページ遷移に引き継がれます）。

### 2. GitHub Pages の有効化（初回のみ）

リポジトリの Settings → Pages → Source を **GitHub Actions** に設定。以後 `main` への push で自動デプロイされます（`.github/workflows/deploy.yml`）。

公開 URL: `https://<ユーザー名>.github.io/LinguaBridge/`

## 開発

```bash
cd web
npm install
npm run dev        # 開発サーバー (http://localhost:5173/LinguaBridge/)
npm test           # ユニットテスト (Vitest)
npm run build      # 型チェック + 本番ビルド
```

## 動作確認（スライス1の受入手順）

1. GAS をデプロイし、中継 URL を設定した状態で公開ページを開く
2. 端末A（先生役）: `teacher.html` → ルーム作成 → 表示された QR を端末B で読む
3. 端末B（生徒役）: 字幕フィードが開くことを確認
4. 端末Aで文を入力して Enter → **5秒以内に端末Bに表示されること**
5. 遅延の実測: 生徒画面を `?debug` 付きで開くと各行に遅延秒数が表示される（console にも常時記録）。同一端末の2タブで測ると時計ずれの影響を受けない
6. 二重表示がないこと・端末Bのリロード後に履歴（直近200文）が復元されることを確認

## 既知の制約（スライス1時点）

- 中継はポーリングのため、配信から表示まで最大3秒程度の遅延が乗る
- ルームは作成から6時間（または GAS キャッシュの都合でそれ以前）に失効する。授業ごとに作り直す想定
- 直近200文を超えた分の履歴は途中参加者には表示されない
- GAS の同時実行制限（30）があるため、多人数での利用前に負荷検証（#7）を行うこと
