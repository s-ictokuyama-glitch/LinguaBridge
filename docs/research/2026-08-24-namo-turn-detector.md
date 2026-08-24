# Namo Turn Detector v1（日本語版）調査 — #31「morph だけで足りるか」（2026-08-24）

対象チケット: wayfinder research #31 / ブランチ `localServer2`
調査方針: 一次情報（Hugging Face のモデルカード・API・`config.json`・`LICENSE`、
GitHub 上の実ソース）のみ。ブログ・ニュース記事は数値の裏取りにのみ使い、
裏が取れなかったものは「未確認」と明記する。
**モデルのダウンロード・pip install は一切行っていない。** よって実測 latency は本書に無い。

---

## 0. 一行サマリ（詳細は各節）

| 問い | 結論 | 確度 |
|------|------|------|
| 日本語専用の重みは実在するか | **実在する。** `videosdk-live/Namo-Turn-Detector-v1-Japanese` | 確認済（HF API） |
| ライセンス | **Apache-2.0**（`LICENSE` に Apache 2.0 全文 + `Copyright 2025 VideoSDK`） | 確認済（実ファイル） |
| 追加ダウンロード量 | **約 140 MB**（うち `model_quant.onnx` が 136 MB） | 確認済（HF API のバイト数） |
| 新規 Python 依存 | **ゼロ。** `onnxruntime` / `transformers` / `tokenizers` / `huggingface_hub` は全て既に `.venv` に入っている | 確認済（ローカル `.venv` 実測） |
| 外部通信 | **モデルを事前配置すれば通信ゼロで動く。** 公式サンプルの `hf_hub_download` を使わなければよい | 確認済（推論コードを読了） |
| 入力 | **テキストのみ**（音声は受け取らない） | 確認済 |
| 出力 | 2値ラベル + softmax confidence | 確認済 |
| 日本語 accuracy | 専用モデル **93.5%** / 多言語モデル **94.36%**（同一 834 サンプル） | 確認済（README・モデルカード） |
| **名詞止め（体言止め）に効くか** | **不明。公表資料に一切の記述・例文・内訳が無く、評価データセットも非公開** | **未確認** |
| CPU latency（Core i5, 実条件） | **未計測。** 公表値は 3 箇所で不一致（<14ms / <19ms / 14.9ms）かつ**測定ハードウェア非開示** | **未確認** |

---

## 1. 入手性とライセンス

### 分かったこと

**日本語専用の重みは実在する。** 多言語1本ではなく、**23言語ぶんの言語別モデル + 多言語モデル1本**という構成。

- 日本語 repo id: **`videosdk-live/Namo-Turn-Detector-v1-Japanese`**
  出典: https://huggingface.co/videosdk-live/Namo-Turn-Detector-v1-Japanese
- 多言語 repo id: `videosdk-live/Namo-Turn-Detector-v1-Multilingual`
- 言語別モデルは 23 本（Arabic / Bengali / Chinese / Danish / Dutch / English / Finnish / French /
  German / Hindi / Indonesian / Italian / **Japanese** / Korean / Marathi / Norwegian / Polish /
  Portuguese / Russian / Spanish / Turkish / Ukrainian / Vietnamese）。
  出典: https://github.com/videosdk-live/NAMO-Turn-Detector-v1 の "Available Models" 節
  および HF API `https://huggingface.co/api/models?author=videosdk-live`

**ライセンスは Apache-2.0。実ファイルで確認済み。**

- HF API の `license` フィールド: `apache-2.0`
- リポジトリ内に `LICENSE`（11,339 バイト）が実在し、中身は Apache License Version 2.0 の全文。
  末尾の著作権表示は `Copyright 2025 VideoSDK`。
  出典: https://huggingface.co/videosdk-live/Namo-Turn-Detector-v1-Japanese/raw/main/LICENSE
- GitHub 側リポジトリも Apache License 2.0。
  出典: https://github.com/videosdk-live/NAMO-Turn-Detector-v1

→ **本プロジェクトの「無料・APIキー不要・再配布可」要件はライセンス面ではクリア。**

**参考: ダウンロード数（採用実績の目安）**
日本語版 655 DL / 多言語版 482 DL / 韓国語版 396,297 DL（HF API, 2026-08-24 時点）。
日本語版は**ほとんど使われていない**。実運用での不具合報告が蓄積されている状態ではない。

### 分からなかったこと

- 学習データの出所・作り方（コーパス名、アノテーション基準）が**どこにも書かれていない**。
  RESEARCH.md にも記載なし。出典: https://github.com/videosdk-live/NAMO-Turn-Detector-v1/blob/main/RESEARCH.md
- 学習/評価データセット `videosdk-live/Namo-Turn-Detector-v1-Train` / `-Test` は
  モデルカードから参照されているが、**HF 上で公開されていない**
  （`https://huggingface.co/api/datasets?author=videosdk-live` は空配列を返す。
  データセットページは HTTP 401）。→ **評価内容を第三者が検証できない。**

---

## 2. サイズと配布形態

### 分かったこと

HF API（`?blobs=true`）で取得した**実バイト数**（推測ではない）:

| ファイル | バイト数 | 用途 |
|---------|---------|------|
| `model.onnx` | 541,442,940 (516.4 MiB) | fp32 版。**不要** |
| **`model_quant.onnx`** | **135,967,547 (129.7 MiB)** | 量子化版。実際に使うのはこれ |
| `tokenizer.json` | 2,919,627 (2.78 MiB) | HF tokenizers 形式 |
| `vocab.txt` | 995,526 (972 KiB) | WordPiece 語彙（1行1トークン） |
| `tokenizer_config.json` | 1,419 | |
| `special_tokens_map.json` | 695 | |
| `config.json` | 630 | |
| `LICENSE` | 11,339 | |
| `README.md` | 7,562 | |
| `confusion_matrices.png` | 122,275 | 参考画像 |

出典: `https://huggingface.co/api/models/videosdk-live/Namo-Turn-Detector-v1-Japanese?blobs=true`

**Parapper が実際に取得しているファイル集合**（`src-tauri/src/model/catalog.rs` の
`NAMO_TURN_DETECTOR_FILES_JAPANESE`）は 6 ファイル:
`config.json`, `model_quant.onnx`, `special_tokens_map.json`, `tokenizer.json`,
`tokenizer_config.json`, `vocab.txt`
→ 合計 **139,885,444 バイト = 133.4 MiB（約 140 MB）**

出典: https://github.com/Parakeet-Inc/Parapper-ASR/blob/main/src-tauri/src/model/catalog.rs

**さらに削れる。** 後述の通り Parapper は日本語版に対して**文字単位トークナイザ**を使い
`vocab.txt` しか読まない（`tokenizer.json` を使わない）。この経路なら実質必要なのは
`model_quant.onnx` + `vocab.txt` = **136,963,073 バイト = 130.6 MiB** のみ。

**int8 であることの確認（算術による裏取り）**
`config.json` は `dim=768 / n_layers=6 / hidden_dim=3072 / n_heads=12 / vocab_size=119547 /
max_position_embeddings=512 / architectures=["DistilBertForSequenceClassification"]`。
出典: https://huggingface.co/videosdk-live/Namo-Turn-Detector-v1-Japanese/raw/main/config.json

これを積むと総パラメータ数は **135,326,210（≒135.3M）**:
- 埋め込み 119,547×768 + 512×768 + LN = 92,206,848
- Transformer 6層 × 7,087,872 = 42,527,232
- pre_classifier 590,592 + classifier 1,538

fp32 なら 541,304,840 バイト。実際の `model.onnx` は 541,442,940 バイト（差分 138KB はグラフ構造）。
**一致する。** そして `model_quant.onnx` は 135,967,547 ≒ 135.3M × 1 バイト。
→ **重みが 1 バイト = int8 動的量子化。** 比は 3.98倍。
公式 RESEARCH.md も「INT8 量子化、精度低下 <0.2%」と記載しており整合する。
出典: https://github.com/videosdk-live/NAMO-Turn-Detector-v1/blob/main/RESEARCH.md

**int8 より小さい版（int4 等）は存在しない。** repo 内の ONNX は fp32 と int8 の 2 つだけ。

**オフライン配布のしやすさ: 高い。**
- Parapper は HF の `resolve/main` を直接 HTTP GET しているだけ
  （`NAMO_TURN_DETECTOR_BASE_URL = "https://huggingface.co/videosdk-live/Namo-Turn-Detector-v1-Japanese/resolve/main"`）。
  つまり**6 ファイルを手でコピーすれば済む**。特殊なローダも認証も不要。
- ローカルディレクトリからの読み込みは `ort.InferenceSession(path)` / Rust `ort` の
  `commit_from_file` で完結する（Parapper 実装がまさにそれ）。

### 分からなかったこと

- HF 以外のミラー（GitHub Releases 等）は**無い**。VideoSDK 公式の配布は HF のみ。
  ただしファイルが独立した静的ファイルなので、社内ミラーを立てるのは容易。
- ファイルの SHA256 は VideoSDK 側からは公表されていない
  （Parapper は Namo に対しては `FileIntegrity` 検証を掛けていない。Vibrato 辞書や
  Supertonic には掛けている）。

---

## 3. 実行方法

### 分かったこと

**入力はテキストのみ。音声は一切受け取らない。**
ASR が出した文字列を投げるだけ。よって LinguaBridge のパイプラインでは
`server/turn/boundary.py` が今 morph 判定をしているのと**まったく同じ位置**に差し込める。

**公式の推論コード全文**（`inference.py`。要点のみ抜粋、コメントは原文ママではない）:

```python
import numpy as np
import onnxruntime as ort
from transformers import AutoTokenizer
from huggingface_hub import hf_hub_download

class TurnDetector:
    def __init__(self, language=None):
        repo_id = get_repo_id(language)                     # ja -> ...-v1-Japanese
        model_path = hf_hub_download(repo_id=repo_id, filename="model_quant.onnx")
        self.tokenizer = AutoTokenizer.from_pretrained(repo_id)
        self.session = ort.InferenceSession(model_path)
        self.max_length = 8192 if language is None else 512

    def predict(self, text: str) -> tuple:
        inputs = self.tokenizer(text, truncation=True,
                                max_length=self.max_length, return_tensors="np")
        outputs = self.session.run(None, {
            "input_ids": inputs["input_ids"],
            "attention_mask": inputs["attention_mask"],
        })
        probabilities = self._softmax(outputs[0][0])
        return np.argmax(probabilities), float(np.max(probabilities))

# result = "End of Turn" if predicted_label == 1 else "Not End of Turn"
```

出典: https://raw.githubusercontent.com/videosdk-live/NAMO-Turn-Detector-v1/main/inference.py

ここから読み取れる事実:

**(a) 入出力**
- 入力 tensor は `input_ids` と `attention_mask` の 2 本のみ。`token_type_ids` は不要。
- 出力は **2 個の logit**。`[not_end, end]` の順。softmax して
  `argmax` が **1 なら "End of Turn"、0 なら "Not End of Turn"**。
  confidence は softmax 確率。**2値 + confidence の両方が取れる。**
  Parapper の Rust 実装も同じ並び順を前提にしている
  （`let [not_end, end, ..] = logits`）。→ **ラベル順序は独立2実装で一致。**

**(b) 前処理**
- 必要なのは WordPiece トークナイズのみ。`truncation=True, max_length=512`。
- **公式コードは padding をしていない**（`padding` 引数なし = 実長のまま渡す）。
  → ONNX グラフの系列長軸は**動的**であると強く推定できる（そうでなければこのコードは動かない）。
  ※ グラフの入力 shape を直接見て確認したわけではないので **未確認**。実測時に要確認。

**(c) Python から動かすのに必要なもの — 新規依存はゼロ**

LinguaBridge の `.venv` を実測した結果:

| パッケージ | 状態 |
|-----------|------|
| `onnxruntime` | **1.27.0 導入済**（sherpa-onnx とは別に単独で入っている） |
| `transformers` | **5.13.0 導入済**（`requirements.txt` に `transformers>=4.40`。NLLB トークナイザ用） |
| `tokenizers` | **0.22.2 導入済**（transformers の依存） |
| `huggingface_hub` | **1.22.0 導入済** |
| `numpy` | 2.5.1 導入済 |

→ **`pip install` は 1 行も要らない。** 増える重い依存も無い（torch は不要。
ONNX 推論なので `transformers` は tokenizer 部分しか使わない）。

さらに、トークナイザは 3 通りの選択肢があり、依存を減らす方向にも振れる:

1. `transformers.AutoTokenizer`（公式方式）— 既に導入済なので追加コストなし
2. `tokenizers.Tokenizer.from_file("tokenizer.json")`— Rust 実装。transformers を import しない
   （前回調査で「冷キャッシュ時の transformers import が pytest の 20s タイムアウトを超える」
   問題が報告されているので、こちらが安全）
3. **`vocab.txt` を dict にして1文字ずつ引く（純 Python、依存ゼロ）**
   — Parapper が日本語版に対して実際に採っている方式。後述 §6。

**(d) 外部通信**
- **モデル本体・推論に通信は無い。** `ort.InferenceSession` はローカルファイルを開くだけ。
- 唯一の通信は `hf_hub_download()` と `AutoTokenizer.from_pretrained(repo_id)`（repo id 渡し）。
  **これらを使わず、事前配置したローカルパスを渡せば通信は完全にゼロ。**
  （`AutoTokenizer.from_pretrained("/local/dir")` はローカルパスならネットに出ない）
- モデル repo 内に実行されるコードは**一切含まれていない**（`.py` ファイルなし、
  `trust_remote_code` を要求する `auto_map` も `config.json` に無い）。
  telemetry・CDN 参照も無い。**通信ゼロ要件と両立する。**
  ※ ただし `transformers` / `huggingface_hub` はデフォルトで匿名テレメトリや
  更新チェックに出ることがあるため、`HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` の
  設定と、本リポジトリ既存の `tests/net_guard.py` による機械検証を併用すべき。

### 分からなかったこと

- ONNX グラフの入力 shape（`[batch, sequence]` の sequence が dynamic axis かどうか）を
  実ファイルで確認していない。**未確認**（§4 のコスト見積もりを大きく左右する）。
- onnxruntime の intra-op スレッド数を上げたときのスケーリング特性。**未計測**。

---

## 4. コスト

### 分かったこと（アーキテクチャと計算量 — ここは確定）

**アーキテクチャ: DistilBERT-base-multilingual-cased ベースの2値系列分類器。**
- `architectures: ["DistilBertForSequenceClassification"]`
- 6層 / hidden 768 / FFN 3072 / 12ヘッド / vocab 119,547 / max_position 512
- **パラメータ数 135.3M**（§2 で算出、fp32 ファイルサイズと一致）
- `vocab_size=119547` は **`distilbert-base-multilingual-cased` の語彙サイズそのもの**。
  つまり「日本語専用モデル」といっても**トークナイザは多言語 mBERT の WordPiece**であり、
  日本語専用の語彙ではない。重みだけが日本語で fine-tune されている。
- 多言語版は別系統で **mmBERT ベース / 295MB / max 8192 tokens**。
  出典: https://github.com/videosdk-live/NAMO-Turn-Detector-v1

**コンテキスト長: 512 トークン上限。** Parapper の `namo_context_max_tokens=256` は
この 512 の内側で「**末尾から 256 トークンだけ残す**」という切り詰め指定
（`trim_to_context` は先頭側を `drain` する = 直近の文脈を優先する）。
設定値は `.min(512)` でクランプされる。

**1回あたりの計算量（実長依存）**

非埋め込み層のパラメータは 42.53M。行列積の演算量は概ね `2 × 42.53M × 系列長`。
これに attention の `QK^T`・`AV`（`6層 × 2 × 2 × L² × 768`）が乗る。

| 系列長 | 概算演算量 | 備考 |
|-------|-----------|------|
| 11 tokens（「じゃあ次、教科書」＝9文字+CLS/SEP） | **0.94 GOP** | |
| 32 tokens（30文字程度の授業発話） | **2.8 GOP** | |
| 128 tokens | **11.3 GOP** | |
| **512 tokens（Parapper の実装）** | **48.4 GOP** | 実長 32 の **17倍** |

→ **重要**: 系列長を実長のまま流せば、これは**極めて軽い**。
授業の1発話は日本語 20〜40 文字が典型なので、実長は 20〜45 トークン程度。
sherpa-onnx の ReazonSpeech K2 int8 encoder（LinguaBridge 実測で固定費 0.027s）と
比べても桁で小さいオーダーになる可能性が高い。

**公表 latency 値（そのまま信用してはいけない）**

| 出典 | 値 | 対象 |
|------|----|------|
| 日本語モデルカード | **< 14 ms** | 言語別モデル |
| GitHub README | **< 19 ms** | 言語別モデル |
| RESEARCH.md | **38 ms → 14.9 ms**（量子化前後） | 言語別モデル |
| GitHub README / RESEARCH.md | **< 29 ms**（61ms → 28ms） | 多言語モデル |

**3 箇所で数値が食い違っており、かつ測定ハードウェアがどこにも書かれていない。**
（RESEARCH.md・README・モデルカード・公式ブログのいずれにも CPU/GPU の記載なし。
「throughput 35.6 → 66.8 tokens/sec」という記述はあるが、これが何のトークンを
指すのかも定義されていない。）

### 分からなかったこと（ここが #31 の最大の穴）

- **Core i5 ノート CPU での実 latency は完全に未計測。** 公表値は使えない。
- **とくに Parapper 実装をそのまま真似ると危険。** Parapper の Rust 実装は
  **常に 512 トークンにゼロパディングし、かつ `with_intra_threads(1)`（1スレッド）**で回す:

  ```rust
  const MAX_SEQUENCE_LEN: usize = 512;
  ...
  while token_ids.len() < MAX_SEQUENCE_LEN {
      token_ids.push(self.pad_id);
      attention_mask.push(0);
  }
  ...
  builder.with_intra_threads(1)
  ```
  出典: https://github.com/Parakeet-Inc/Parapper-ASR/blob/main/src-tauri/src/recognition/turn/decision/engine.rs

  attention_mask で 0 にしても、**行列積の計算量は減らない**（密行列のまま 512 長を計算する）。
  つまり Parapper は上表の 48.4 GOP を毎回 1 スレッドで払っている。
  これが Core i5 で何 ms なのかは**測らないと分からない**が、
  公表値の 14ms は明らかにこの条件のものではない。

  **→ LinguaBridge 側で実装する場合、512 パディングは真似る必要がない**
  （公式 Python コードは実長で流している）。ここは実装選択で 1桁以上変わりうる。

- 512 パディングが必要か不要かは、ONNX グラフの dynamic axis 次第。**未確認**（§3(b)）。
- モデルロード時間（135MB の int8 ONNX の `InferenceSession` 初期化）**未計測**。
- 常駐メモリ（135MB のモデル + arena）**未計測**。
  LinguaBridge は既に ReazonSpeech K2 + Hy-MT2-1.8B Q4 を常駐させているので、
  ここは無視できない可能性がある。

---

## 5. 日本語での効果の実測値

### 分かったこと

**公表されている日本語の数値は accuracy 系のみ。**

日本語専用モデル（`Namo-Turn-Detector-v1-Japanese`、モデルカード記載）:

| 指標 | 値 |
|------|----|
| Accuracy | **0.935252（93.53%）** |
| F1 | 0.938776 |
| Precision | **0.896104** |
| Recall | **0.985714** |

評価データ: 「800+ Japanese utterances from diverse conversational contexts」
（`videosdk-live/Namo-Turn-Detector-v1-Test` 由来と記載）
出典: https://huggingface.co/videosdk-live/Namo-Turn-Detector-v1-Japanese

多言語モデルの日本語行（GitHub README の 23言語ベンチ表、"Performance Benchmarks for
**Multilingual** Model" 節）:

| Language | Accuracy | Precision | Recall | F1 | Samples |
|----------|----------|-----------|--------|----|---------|
| Japanese | **94.36%** | 0.9099 | **0.9857** | 0.9463 | **834** |

出典: https://github.com/videosdk-live/NAMO-Turn-Detector-v1

**注目すべき点が3つある。**

1. **多言語モデル（94.36%）のほうが日本語専用モデル（93.53%）より日本語で強い。**
   両者の Recall が 0.9857 / 0.985714 と**完全に一致**しており、
   0.9436 × 834 ≈ 787、0.935252 × 834 ≈ 780 と**サンプル数 834 で整合**する。
   → **同一の 834 サンプル日本語テストセット上での比較**とみてよい。
   Precision だけが 0.8961 → 0.9099 と改善している。
   つまり「日本語専用だから日本語に強い」という前提は**この数値上は成り立たない**。
   （ただし多言語版は 295MB / mmBERT で、サイズは 2.2 倍になる。）

2. **Precision が低く Recall が高い、という非対称。**
   ラベル 1 = End of Turn なので、
   - Recall 0.986 = **本当に終わっている発話は、ほぼ取りこぼさない**
   - Precision 0.896 = **「終わった」と言ったうちの 10.4% が誤り**（＝まだ続く発話を切ってしまう）

   これは **#31 の懸念（名詞止めを終了と認めてほしい）に対しては追い風、
   ただし過剰分割のリスクを新たに持ち込む**方向のバイアスである。
   Parapper が `namo_confidence_threshold` を既定 0.7 より高い **0.8** に上げているのは、
   この Precision の低さを実運用で締めた結果だと解釈するのが自然だが、
   **その意図が明記された一次情報は見つかっていない（推測）**。

3. **833/834 サンプルという規模は小さい。** 93.5% と 94.4% の差は 7 サンプル程度であり、
   統計的に有意と言い切れる規模ではない。

### 分からなかったこと — **ここが致命的**

- **「名詞止め・体言止め」に関する記述・例文・内訳は、一次情報のどこにも存在しない。**
  確認した範囲: 日本語モデルカード / GitHub README / RESEARCH.md / 公式ブログ2本。
  日本語の例文は**1つも掲載されていない**（公式 `inference.py` のサンプル文も全て英語）。
- **false endpoint / delayed endpoint / turn-taking error といった対話特有の指標は
  一切公表されていない。** 公表されているのは accuracy / precision / recall / F1 だけ。
  RESEARCH.md にも「評価方法論、混同行列、エラー内訳は記載なし」。
- **VAD のみ・文法ルールのみとの比較値も公表されていない。**
  公式ブログは「無音ベース VAD の限界」を文章で論じているが、
  **比較表・改善幅の数値は載せていない**（確認済）。
  出典: https://www.videosdk.live/blog/namo-turn-detection-v1-semantic-turn-detection-for-ai-voice-agents
- **評価データセットが非公開**（§1）のため、834 サンプルの中に体言止め発話が
  どれだけ含まれるか、そもそも含まれるかを**第三者は検証できない**。
- 評価データが「conversational contexts」であることは書かれているが、
  **教室の日本語（教師の一方向発話、敬体中心、教科用語）とドメインが一致する保証は無い。**

**→ #31 の中核の問い「名詞止めに効くか」は、公表資料からは一切答えられない。**

---

## 6. Parapper 側の使い方（Rust ソースを実読）

Parapper-ASR は MIT でソース公開されており、該当箇所を実際に読んだ。

### 6-1. 設定値の定義と既定

`src-tauri/src/config/settings.rs`:

```rust
Self {
    detector: TurnDetector::Simple,      // 既定は Simple（Namo でも Morph でもない）
    interim_result_enabled: true,
    interim_result_silence_ms: 96,
    check_silence_ms: 320,
    namo_confidence_threshold: 0.8,      // ← #31 に出てくる値
    namo_context_max_tokens: 256,        // ← #31 に出てくる値
    rerecognize_full_on_complete: false,
}
```
正規化時に `namo_confidence_threshold.clamp(0.0, 1.0)`、
`namo_context_max_tokens.min(512)` が掛かる。
出典: https://github.com/Parakeet-Inc/Parapper-ASR/blob/main/src-tauri/src/config/settings.rs

**前回調査（`2026-08-23-parapper-whisperflow-analysis.md` R-2）で報告した
`namo_confidence_threshold=0.8` / `namo_context_max_tokens=256` は、実ソースで裏が取れた。**

### 6-2. 「NormalEnd または候補なしのときだけモデルに委ねる」の実装

3つのファイルに分かれている。**チケット記載の理解は正しい。**

**(1) 戦略スイッチ** — `src-tauri/src/config/turn.rs`

```rust
pub enum TurnDetector { Simple, Morph, Namo }

pub fn uses_morph_boundary(self) -> bool { matches!(self, Self::Namo | Self::Morph) }
pub fn confirms_normal_end_with_namo(self) -> bool { matches!(self, Self::Namo) }
```
→ **Namo 戦略は Morph の上位互換。** 文法境界判定は常に先に走り、
Namo は `NormalEnd` の確定判断だけを肩代わりする。

**(2) 文法境界からの分岐** — `src-tauri/src/recognition/turn/policy/grammar.rs`

```rust
pub fn action_after_rerecognition(candidates: Vec<Candidate>,
                                  no_candidate_action: NoCandidateAction) -> Action {
    if candidates.is_empty() {
        return match no_candidate_action {
            NoCandidateAction::DecideWithNamo => Action::DecideWithNamo,     // Namo戦略
            NoCandidateAction::ContinueOpen  => Action::ContinueOpen { emit_interim: true },
        };
    }
    // 末尾にある候補だけを見る（文中の境界では絶対に切らない）
    let Some(evaluated) = candidates.into_iter().rev()
        .find(|c| c.is_at_text_end) else { return Action::ContinueOpen { emit_interim: true } };
    if candidate_is_confirmed(&evaluated) { return Action::CompleteTurn; }
    Action::ContinueOpen { emit_interim: true }
}

fn candidate_is_confirmed(c: &Candidate) -> bool {
    match c.class {
        StrongEnd | PredicateEnd => true,                  // Namo を呼ばずに確定
        NormalEnd                => c.normal_end_is_confirmed,  // ← Namo の答えが入る
        Reject | ClauseWeak      => false,                 // Namo を呼ばずに継続
    }
}
```

**(3) Namo を呼ぶ条件の実体** — `src-tauri/src/recognition/turn/boundary_flow.rs`

```rust
let normal_end_is_confirmed =
    if is_at_text_end && matches!(candidate.class, GrammarBoundaryClass::NormalEnd) {
        if confirm_normal_end_with_namo {
            let text = slice_chars(combined_text, 0..candidate.char_end);
            self.namo_final_decision_for_text(turn_id, &text)   // ← ここだけでモデルを叩く
        } else { false }
    } else { false };
```

**確定した呼び出し条件（2条件のみ）:**
1. **文法境界候補が末尾にあり、そのクラスが `NormalEnd`**（＝名詞・代名詞・接尾辞・感動詞止め）
2. **文法境界候補が1つも無いとき**（`NoCandidateAction::DecideWithNamo`）

`StrongEnd` / `PredicateEnd` は Namo を**呼ばずに**即確定、
`Reject` / `ClauseWeak` は Namo を**呼ばずに**継続。
→ **Namo は「morph が判断を保留した箇所だけ」に走る。呼び出し頻度は低い。**

そして `NormalEnd` は、LinguaBridge の `server/turn/boundary.py` でも
`BoundaryClass.NORMAL_END`（名詞・感動詞など → morph 戦略なら継続）として
同じ意味で実装済み。**#31 の「名詞止め懸念」は、Parapper では Namo に投げる箇所そのもの。**

### 6-3. しきい値の適用位置 — 「二重ゲート」

`src-tauri/src/recognition/turn/flow.rs`:

```rust
self.io.turn_decision_runner
    .decide(route, text, self.config.turn.namo_context_max_tokens)
    .is_ok_and(|decision| {
        decision.is_end_of_turn                                   // ゲート1
            && decision.confidence >= self.config.turn.namo_confidence_threshold  // ゲート2
    })
```

そして `decision/engine.rs` 側:

```rust
let end_probability = softmax_second(*not_end, *end);
NamoTurnDecision {
    is_end_of_turn: end_probability >= 0.5,   // ゲート1 の中身
    confidence: end_probability,               // ← argmax の確率ではなく「End 側の確率」
}
```

**重要な差異**: 公式 Python は `confidence = max(probabilities)`（argmax 側の確率）だが、
Parapper は **`confidence = P(End)` に固定**している。
よってゲート2 は実質「**P(End) ≥ 0.8**」の単一条件（ゲート1 の `≥ 0.5` は包含される）。
→ **「終了」と言うには 80% 以上の確信を要求する = 継続側に倒す保守的設定。**
§5 で見た Precision 0.896 の低さを締めるチューニングとして整合する。

**エラー時は継続。** `.is_ok_and(...)` なので、
モデルロード失敗・推論失敗は `false` = **継続扱い**にフォールバックする（fail-safe）。

### 6-4. `Continue` 応答とタイムアウト

`Continue` のときは `keep_turn_open()` が走る（`flow.rs`）:

```rust
pub fn keep_turn_open(&mut self, turn_id: u64, emit_interim: bool) {
    self.turn_store.open_turn_id = Some(turn_id);
    self.turn_store.open_turn_accepts_root_segment = true;
    self.reset_open_turn_timeout_origin();          // ← タイムアウト起点をリセット
    if emit_interim && self.config.turn.interim_result_enabled {
        self.emit_turn_output(turn_id, false);      // interim 字幕は出す
    }
}
```

タイムアウトの長さは `policy/timeout.rs`:

```rust
pub fn ticks(config: &ParapperConfig) -> u64 {
    let vad_interval_ms = u64::from(config.segmentation.vad_interval_ms).max(1);   // 32
    let timeout_ms = u64::from(config.turn.check_silence_ms).saturating_mul(2);    // 320 * 2 = 640
    timeout_ms.div_ceil(vad_interval_ms).max(1)                                    // 20 ticks
}
```

**→ Parapper の「Namo が Continue と言った後の待ち」は `check_silence_ms × 2 = 640ms`。**
LinguaBridge の `force_silence_ms = 1000ms`（`config.yaml`）より**短い**。
しかもタイムアウトは「新しい音声活動が来たらリセット」される
（`open_turn_activity_epoch != segment_activity_epoch` → `ResetTimeoutOrigin`）ので、
実際には**無音が 640ms 続いたときだけ**発火する。

**この構造が意味すること（#31 に直結）:**
Namo は「名詞止めの待ち時間をゼロにする」わけではない。
**Namo が P(End) ≥ 0.8 を返したときだけ待ちが 0 になり、返さなければ 640ms 待つ。**
つまり期待できる改善は「名詞止め発話のうち Namo が高信頼で終了と言えた割合 × 待ち時間」。

### 6-5. トークナイザ選択 — 日本語版だけ「文字単位」

`src-tauri/src/recognition/control/engine_cache.rs`:

```rust
let tokenizer_kind = match model {
    NamoTurnDetectorModel::Japanese => NamoTokenizerKind::Character,      // ← 文字単位！
    NamoTurnDetectorModel::English | NamoTurnDetectorModel::Multilingual =>
        NamoTokenizerKind::TokenizerJson,
};
```

`Character` の実装（`decision/engine.rs`）は
「`vocab.txt` を行番号→ID の HashMap にして、**空白を除いた1文字ずつ引く**、
未知文字は `[UNK]`、前後に `[CLS]`/`[SEP]`」という素朴なもの。

**この事実は2つの意味を持つ。**

1. **良い面**: LinguaBridge で実装するなら、**トークナイザライブラリすら不要**。
   `vocab.txt`（972KB）を読んで dict を作るだけの純 Python 数十行で済む。
   `transformers` の import コスト（前回調査で pytest 20s タイムアウト超過の実績あり）を回避できる。
2. **注意すべき面**: これは**公式の推論方法ではない**。
   公式は mBERT WordPiece（`tokenizer.json`）を使う。
   日本語（漢字・かな）については mBERT の WordPiece が実質的に文字単位に分割するため
   ほぼ一致するが、**ラテン文字・数字・記号（「A組」「3時間目」など）では ID 列が食い違う。**
   → **Parapper 経路で使うと、公表 accuracy 93.5% がそのまま出る保証はない。**
   Parapper 自身がこの選択の根拠を書いた資料は見つからなかった（**未確認**）。

### 分からなかったこと（Parapper 側）

- Parapper が Namo 戦略で実際にどれだけ改善したかの**実測値は公開されていない**。
  README は「短い間で字幕が分断されにくくなる」と定性的に述べるのみ。
- `namo_confidence_threshold` を 0.7（コード内テスト JSON の値）ではなく
  0.8（既定値）にした根拠。**未確認**。
- 日本語に `Character` トークナイザを選んだ根拠。**未確認**。
- Parapper の既定 `TurnDetector` は **`Simple`**（VAD のみ）であり、Morph でも Namo でもない。
  つまり**開発元自身が Namo を既定にしていない**。理由は不明。

---

## 7. LinguaBridge の現状への接続

本リポジトリで既に確定している事実（#28・#24 実測）と突き合わせる。

| LinguaBridge の現状 | Namo を足したときの差分 |
|---------------------|------------------------|
| morph 既定（`server/turn/boundary.py`、5分類は Parapper と同一設計） | **上位互換として乗る。** morph を捨てる必要はない。`NORMAL_END` 分岐だけ差し替え |
| 合成音源17クリップで不自然分割 **3件→0件**（改善上限が既に 0） | **この17クリップでは 1件も改善しない**（morph が既に全部正しく切っている） |
| ReazonSpeech K2 v2 は句読点を出さないが、敬体 `〜です/〜ます` で `predicate_end` が効く | **敬体の発話には Namo の出番が無い**（`PredicateEnd` は Namo を呼ばずに即確定） |
| ASR 固定費 1.088s → 0.027s | Namo の推論費が 0.027s と同オーダーなら固定費が実質倍増しうる。**未計測** |
| `force_silence_ms = 1000ms` のタイムアウト待ち | Namo が P(End)≥0.8 を返した名詞止め発話でのみ **最大 1000ms 短縮** |
| 名詞止め発話は**合成音源に1件も無い** | **効果を測る素材が無い**（Namo を入れても改善を観測できない） |
| 絶対要件: 完全ローカル / 無料 / APIキー不要 / GPU不要 / 通信ゼロ / Windows CPU | **ライセンス・依存・通信の観点では全てクリア**（§1, §3） |

---

## 8. この情報で #31 の採否は決まるか

**採否の判断そのものは本書の役目ではない。** 事実の充足度だけを述べる。

### 8-1. 「この事実だけで不採用と言い切れるか」— 言い切れない

不採用の典型的な理由は、いずれも**成立しない**:

| 却下理由の候補 | 成立するか | 根拠 |
|---------------|-----------|------|
| 日本語版が存在しない | **成立しない** | `videosdk-live/Namo-Turn-Detector-v1-Japanese` が実在（§1） |
| ライセンスが使えない | **成立しない** | Apache-2.0 全文を実ファイルで確認（§1） |
| サイズが過大 | **成立しない** | 136MB（最小構成 131MB）。既に同居している Hy-MT2-1.8B Q4 や ReazonSpeech より小さい（§2） |
| 依存が重い | **成立しない** | **新規依存ゼロ。** `onnxruntime` 1.27.0 / `transformers` 5.13.0 / `tokenizers` 0.22.2 が既に `.venv` に導入済（§3） |
| 通信が発生する | **成立しない** | 推論に通信なし。事前配置で完結。repo に実行コードなし（§3d） |
| GPU が要る | **成立しない** | int8 ONNX / CPU 前提の設計（§4） |
| 設計に噛み合わない | **成立しない** | Parapper の `NormalEnd` 分岐は LinguaBridge の `BoundaryClass.NORMAL_END` と同じ位置（§6-2, §7） |

**→ 「調べた結果ダメだった」という形での即時不採用は、本調査からは導けない。**

### 8-2. 「この事実だけで採用と言い切れるか」— これも言い切れない

**#31 の中核の問い（名詞止めに効くか）に、一次情報は答えを持っていない。**

決定的に欠けているのは 2 つだけ:

**(A) 名詞止め日本語発話での実効果 — 完全に未知**
- モデル作者は日本語の例文を1つも公開していない
- 体言止め・常体に関する記述がゼロ
- false endpoint / delayed endpoint 指標がゼロ
- VAD のみ・文法のみとの比較値がゼロ
- 評価データセットが非公開で第三者検証不能
- 公表されているのは 834 サンプルの accuracy 93.5% のみ。
  ただし **Precision 0.896 / Recall 0.986 という非対称は「終了と言いたがる」バイアス**を
  示しており、名詞止めに対しては**理屈の上では追い風**（§5）。これは推論であって実測ではない。

**(B) Core i5 での実 latency — 完全に未知**
- 公表値が 3 箇所で不一致（<14ms / <19ms / 14.9ms）かつ測定ハードウェア非開示
- Parapper 実装は常に 512 パディング × 1スレッド（実長 32 の 17倍の演算量）
- ただし公式 Python は実長で流しており、そちらなら 0.94〜2.8 GOP と非常に軽い可能性が高い
- ONNX グラフの dynamic axis を未確認のため、どちらが可能か確定していない

### 8-3. 足りないものを埋めるには何が要るか

**必要なのは追加の文献調査ではない。文献はもう尽きている。**
残りは全て**このリポジトリで測れば決まる**性質のもの。

1. **名詞止め・常体を含む評価素材を作る**（最優先。これが無いと Namo の有無に関わらず何も測れない）
   - 現在の合成音源17クリップには名詞止めが1件も無い。
   - 「じゃあ次、教科書」「はい、これ」「明日は実験だ」「これが答えである」等を
     `tests/fixtures/ja_corpus.json` 相当に追加し、**まず morph 単体の失敗件数を確定させる**。
   - **もし morph 単体でも失敗が 0 件なら、Namo の改善上限はここでも 0 になり、#31 は自動的に決着する。**
     この検証は **Namo をダウンロードせずに実行できる。**

2. **(1) で morph の失敗が観測できた場合のみ**、Namo を 136MB 落として測る:
   - 失敗した名詞止め文字列を直接 Namo に投げ、P(End) が 0.8 を超えるか
   - 実長入力（padding なし）が通るか＝ ONNX の dynamic axis 確認
   - 実長入力と 512 パディングそれぞれの Core i5 latency
   - 逆方向の副作用: 現在 morph が正しく継続扱いしている `NormalEnd` 発話を
     Namo が誤って切らないか（Precision 0.896 のリスク）
   - `Character` トークナイザ（Parapper 方式・依存ゼロ）と
     `tokenizer.json` 方式で判定が食い違わないか

3. 併せて検討に値する分岐:
   - **多言語版（295MB / mmBERT）のほうが同一日本語テストセットで 94.36% > 93.53%**（§5）。
     サイズ 2.2 倍を許容するなら、日本語専用版より多言語版が候補になりうる。
     この逆転は一次情報で確認済みだが、834 サンプルでの ~7 件差であり有意性は不明。

---

## 出典一覧

**Hugging Face（一次）**
- モデルカード: https://huggingface.co/videosdk-live/Namo-Turn-Detector-v1-Japanese
- ファイル一覧・バイト数・ライセンス: `https://huggingface.co/api/models/videosdk-live/Namo-Turn-Detector-v1-Japanese?blobs=true`
- `config.json`: https://huggingface.co/videosdk-live/Namo-Turn-Detector-v1-Japanese/raw/main/config.json
- `LICENSE`: https://huggingface.co/videosdk-live/Namo-Turn-Detector-v1-Japanese/raw/main/LICENSE
- 全モデル一覧: `https://huggingface.co/api/models?author=videosdk-live`
- データセット一覧（空を確認）: `https://huggingface.co/api/datasets?author=videosdk-live`

**VideoSDK 公式 GitHub（一次）**
- https://github.com/videosdk-live/NAMO-Turn-Detector-v1
- https://github.com/videosdk-live/NAMO-Turn-Detector-v1/blob/main/RESEARCH.md
- https://raw.githubusercontent.com/videosdk-live/NAMO-Turn-Detector-v1/main/inference.py

**Parapper-ASR ソース（一次、MIT）**
- `src-tauri/src/config/turn.rs` — https://github.com/Parakeet-Inc/Parapper-ASR/blob/main/src-tauri/src/config/turn.rs
- `src-tauri/src/config/settings.rs` — https://github.com/Parakeet-Inc/Parapper-ASR/blob/main/src-tauri/src/config/settings.rs
- `src-tauri/src/recognition/turn/decision/engine.rs` — https://github.com/Parakeet-Inc/Parapper-ASR/blob/main/src-tauri/src/recognition/turn/decision/engine.rs
- `src-tauri/src/recognition/turn/boundary_flow.rs` — https://github.com/Parakeet-Inc/Parapper-ASR/blob/main/src-tauri/src/recognition/turn/boundary_flow.rs
- `src-tauri/src/recognition/turn/flow.rs` — https://github.com/Parakeet-Inc/Parapper-ASR/blob/main/src-tauri/src/recognition/turn/flow.rs
- `src-tauri/src/recognition/turn/policy/grammar.rs` — https://github.com/Parakeet-Inc/Parapper-ASR/blob/main/src-tauri/src/recognition/turn/policy/grammar.rs
- `src-tauri/src/recognition/turn/policy/timeout.rs` — https://github.com/Parakeet-Inc/Parapper-ASR/blob/main/src-tauri/src/recognition/turn/policy/timeout.rs
- `src-tauri/src/recognition/turn/policy/completion.rs` — https://github.com/Parakeet-Inc/Parapper-ASR/blob/main/src-tauri/src/recognition/turn/policy/completion.rs
- `src-tauri/src/recognition/control/engine_cache.rs` — https://github.com/Parakeet-Inc/Parapper-ASR/blob/main/src-tauri/src/recognition/control/engine_cache.rs
- `src-tauri/src/model/catalog.rs` — https://github.com/Parakeet-Inc/Parapper-ASR/blob/main/src-tauri/src/model/catalog.rs

**二次情報（数値の裏取りにのみ使用。単独の根拠としては採用していない）**
- https://www.videosdk.live/blog/namo-turn-detection-v1-semantic-turn-detection-for-ai-voice-agents

**本リポジトリ内**
- `docs/research/2026-08-23-parapper-whisperflow-analysis.md`（R-2 / R-3 / R-4）
- `server/turn/boundary.py`, `server/turn/assembler.py`, `server/config.py`, `config.yaml`
- `.venv` のパッケージ実測（onnxruntime 1.27.0 / transformers 5.13.0 / tokenizers 0.22.2 / huggingface_hub 1.22.0）
