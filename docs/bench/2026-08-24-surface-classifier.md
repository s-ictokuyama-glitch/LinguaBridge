# 表層分類器: 常体・漢字表記を足した A/B 実測（#34）

- 機材: AMD Ryzen 7 8840U w/ Radeon 780M Graphics / 8C16T / RAM 16.4GB
- 日時: 2026-08-24
- 音源: `tests/fixtures/ja_ext/`（#22 の17クリップ + #34 で足した `taigen-01..03` / `joutai-01..03`）
- ASR: sherpa-onnx + ReazonSpeech K2 v2（#28 の出荷既定）
- 再現: `python scripts/tune_turn.py --classifier-ab`

## 1. 分割を増やさずに取りこぼしを減らせたか

| 条件 | カード数 | 不自然な分割 | 本当の切れ目 | 文法で確定できず | その待ちの合計 | 全turnの待ち 中央/最大 |
|---|---|---|---|---|---|---|
| A simple | 35 | **5** | 8/8 | 0 | 0ms | 500ms / 512ms |
| B before | 27 | **0** | 5/8 | 10 | 10000ms | 500ms / 1000ms |
| C after | 30 | **0** | 8/8 | 6 | 6000ms | 500ms / 1000ms |

**「不自然な分割」が1件でも増えたら不採用**（#27 の不変条件「文中の境界では絶対に分割しない」）。「文法で確定できず」は末尾のクラスで Turn を確定できず、無音の経過（`boundary` / `flush`）で確定した Turn の数。1件につき 1000ms 待つので、字幕がそのぶん遅れる。

`turn_wait_ms_*` は #34 で足した指標。`endpoint_latency_ms` は音源終端の flush 確定を数えていないため、**取りこぼしの代償がそこに出てこなかった**（#27 の申し送り）。定義を変えると #24/#27/#28 と比較できなくなるので、既存の指標はそのままに別の列を足してある。

## 2. 代償（ASR呼び出しとデコード時間）

| 条件 | ASR呼び出し | decode合計(中央値) | endpoint latency 中央/最大 | Hy-MT2呼び出し |
|---|---|---|---|---|
| A simple | 35 | 4.62s | 512ms / 512ms | 70 |
| B before | 35 | 4.62s | 512ms / 1024ms | 54 |
| C after | 35 | 4.61s | 512ms / 1024ms | 60 |

分類器は ASR の後段なので **ASR 呼び出しは変わらないのが正しい**（変わっていたら Segment の切り方に手が入っている）。

## 3. クリップごとに何が変わったか（before → after）

- **pause-03**（改善）: 2枚/0件/待ち1512ms → 2枚/0件/待ち1012ms
    - `predicate_end` seg=1 wait=512ms: 昨日の実験の結果をグループごとに発表してもらいます
    - `predicate_end` seg=1 wait=500ms: 準備ができた班から前に出てきて下さい
- **taigen-01**（改善）: 1枚/0件/待ち1000ms → 1枚/0件/待ち500ms
    - `predicate_end` seg=3 wait=500ms: じゃあ次教科書四重にページを開いて下さい
- **joutai-01**（改善）: 1枚/0件/待ち1000ms → 2枚/0件/待ち1512ms
    - `predicate_end` seg=1 wait=512ms: これが光合成だ
    - `flush` seg=1 wait=1000ms: よく覚えておくように
- **joutai-02**（改善）: 1枚/0件/待ち1000ms → 2枚/0件/待ち1012ms
    - `predicate_end` seg=1 wait=512ms: ここが大事だな
    - `predicate_end` seg=1 wait=500ms: よしはじめるぞ
- **joutai-03**（改善）: 1枚/0件/待ち1000ms → 2枚/0件/待ち1012ms
    - `predicate_end` seg=1 wait=512ms: 分かったか
    - `predicate_end` seg=1 wait=500ms: 分からない人は手を挙げて下さい

### 分類の内訳

| 条件 | 確定理由の内訳 |
|---|---|
| A simple | `{'segment': 35}` |
| B before | `{'predicate_end': 17, 'flush': 8, 'boundary': 2}` |
| C after | `{'predicate_end': 24, 'flush': 4, 'boundary': 2}` |

ReazonSpeech は句読点を出さないので `strong_end` は**構造的に0件**（#21/#28）。評価は `predicate_end` / `normal_end` / `reject` の3クラスで見る。

