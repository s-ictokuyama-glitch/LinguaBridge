# Haas ONNX の認識進行と字幕継続

有限の音声を認識し終え、次の音声も翻訳字幕まで処理できることを検査する。
現行実装では反例を検出していない。[基準結果](haas_decode_baseline.json) と
[実行手順](../../.agents/skills/haas-decode/SKILL.md) を参照する。

## この境界を選んだ理由

出荷設定 `config.yaml` は `haas-onnx` の int8 を使う。
既存の `continuous-speech`、`silero-lesson`、`morph-lesson` は認識計算を代役へ置換する。
実モデルの通常入力を反復する `lesson_liveness_real.py` も、モデル出力の各分岐は列挙しない。
Haas のdurationが0で文字が出続ける場合の進行条件が壊れると、認識が期限まで返らず、後続字幕が滞る。

PythonとNumPyの実コードへ有限入力を与える方法を選んだ。
別言語に認識ループを写すモデルよりも、実際の分岐・generator・配列切出しを検査できる。
SMTによる証明、任意サイズの証明、ONNX計算の形式検証ではない。

先生ブラウザーの送信停滞も候補だった。既存検証ではWebSocketの送信buffer容量を除外しているが、
停滞時の容量・破棄方針はまだ明確でない。今回は既定授業が必ず通り、進行規則をコードで確認できる認識処理を優先した。

## 認識ループの全列挙

`_HaasDecoder._decode` と `_predict` を実行し、ONNXのjointとpredictionのセッションを代役にする。
`Decision(token, duration)` が1回のjoint出力を表す。
predictionは状態を次へ渡す契約だけを模擬し、実ニューラルネットワークの計算は行わない。

| 項目 | 範囲 |
|---|---|
| encoded frame数 | 0、1、2 |
| token | 257「です」、3072 blank |
| duration | 0、1、2、3、4 |
| 同一frameの文字数上限 | 出荷モデルと同じ10 |
| 終端までの全出力列 | 0 frameで1列、1 frameで91列、2 frameで2,881列、合計2,973列 |

次のjoint出力が必要になった場所だけで10通りに分岐し、実コードを先頭から再実行する。
実コードが終了した後の接尾辞は作らない。固定長20判断の直積より小さく、到達する全分岐を調べられる。
各終端列を同じデコーダーで再実行する。全語彙や全asyncio順序の網羅は意味しない。

観測したjoint呼出しから、次の性質を確認する。

- 同じframeへの呼出しは10回以下、全体ではframe数×10回以下。
- 正のdurationは指定した幅を進む。blankでdurationが0なら1進む。非blankでdurationが0なら10文字目で1進む。
- generatorの出力とpredictionの呼出しは、観測した非blankのtoken列と一致する。
- predictionが返した状態を次の呼出しへ渡し、認識開始時は同じ初期状態を使う。
- 最終のframeを消費する前に認識を終了しない。

呼出し上限はjointの入口で判定する。無限ループのタイムアウトを「破壊を検出した」と数えない。
固定8回の追加検査では、最大5frame、40回のduration=0、複数token「光」「です」「。」、異なる先行発話後の再実行を扱う。
文字列変換はこの3tokenのみ。未知語、byte fallback、Unicode全般は対象外。

設定は固定モデル `common/decoding.json` から確認した。
SHA256は `e535e51bed8729c3d1c5acb5d016feae774f8b13160f2dccbcd4841ad6ecd5be`。
製品のmanifest固定値は `server/asr/haas_onnx_engine.py` の `MANIFEST_SHA256` にある。
`_HaasDecoder.__init__` とモデル検証・ロードはこの検査では実行しない。
モデル設定を変更した場合は、数値とtoken対応を現物と再照合する必要がある。

## PCM分割と字幕への結合

`_chunks` には11種類の長さと3種類の波形の全組合せ、33ケースを与える。
長さは1、319、320、447999、448000、448001、479999、480000、480001、896000、960001サンプル。
波形は周期65536の整数列、一定値、切断探索区間の一部を無音にした整数列。
28秒・30秒前後と約60秒の入力を含む。出力を連結して入力PCMと全値比較し、各断片が非空で30秒以下であることを確認する。
さらに実 `HaasOnnxEngine.transcribe` を通し、float32への正規化と断片ごとの文字列連結を検査する。
この段階の内部decoderは入力を保存する代役で、認識内容の期待値を入力から独立に用意する。

字幕への結合では短いPCMを使い、実 `Pipeline.feed_audio`、energy VAD、既定morph、実 `HaasOnnxEngine.transcribe`、
`_features`、`_decode`、`_text`、字幕配信と送信キューを通す。
13発話を200msの声と500msの無音で入力し、4回の空認識と4回のduration=0上限到達を挟む。
発話ごとのdrainは行わず、末尾の排出後に先生の原文と英中各9字幕の内容・順序・seqを比較する。
最後の正常発話が届くことを確認する。長PCMの分割検査とは別のケースであり、50分連続入力や実時間の到着期限を検査するものではない。

ONNXのencoder出力は呼出し順の台帳から返す。入力の音声内容と認識文の対応は保証しない。
特徴量計算には4帯域の合成filterと合成window、prediction状態には1×1配列を使う。
出荷frontendの数値一致、実state形状、encoder配列長と `enc_length` が異なるパディング境界は対象外。
MTは `en:原文`、`zh:原文` を返す代役。マイク、Silero、モデル精度、ONNX内部の停止、実WebSocket、LAN、生徒画面は含まない。

## 意図的な破壊を検出する

`--mutation-check` は一時コピーの製品コードへ12種類の破壊を個別に入れる。
終了コード1と期待する `property` を必須とし、Traceback・置換不一致・子プロセスの期限超過は `ERROR` にする。
元のソースのSHA256と全検証の結果が復元後にも一致することを確認する。

| 破壊 | 診断 |
|---|---|
| 10文字での強制前進を削除 | `decode_progress` |
| blankの最小前進を削除、durationの幅を無視 | `frame_order` |
| 最終prediction状態を次の認識へ持ち越す | `utterance_reset` |
| blankも出力 | `token_preservation` |
| 最終frameを処理せず終了 | `decode_completion` |
| token文字列を欠落 | `token_text` |
| 分割境界の1サンプルを欠落・重複 | `pcm_partition` |
| PCMの正規化倍率を変更 | `pcm_normalization` |
| 断片の認識文字列を欠落 | `chunk_text` |
| 認識文を空にして字幕へ渡す | `caption_continuation` |

代表的な反例は、frame 0で非blank・duration 0が10回続き、11回目もframe 0を読む順序。
正常版は10文字でframe 1へ進む。破壊版は `decode_progress` で停止する。
PCM欠落の破壊では448001サンプル入力が448000サンプルになり、独立した入力台帳との比較に失敗する。
これらは検出器の確認用に作った反例であり、現行製品の不具合ではない。
