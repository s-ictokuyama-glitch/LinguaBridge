# NLLB の反復出力と採用範囲

Issue #53 の英訳反復は、既存の int8 モデルと beam 幅 1 で再現した。
公式手順と同じ入力でも発生する。現在の NLLB を意味精度で合格とは扱わず、既定は Hy-MT2 のままにする。
NLLB は速度優先の任意選択として残す。授業で使う挨拶と用語を英語・中国語の両方で確認してから使う。

## 比較した推論条件

基点は `localServer2` の `5d2f254`。Windows、CPU、CTranslate2 4.8.1、Transformers 5.13.0、tokenizers 0.22.2、sentencepiece 0.2.1。
モデルは `JustFrederik/nllb-200-distilled-600M-ct2-int8`、tokenizer は `facebook/nllb-200-distilled-600M`。

配布元モデル revision は `302d78f00e6fdb50a1064059df7c392b735e9d05`。
開発機の `model.bin` は 622595991 bytes、SHA-256 は `ed1beaf75134de7505315a5223162f56acff397eff6b50638a500d3936fe707b`。
Hugging Face の公開 LFS メタデータと一致した。重みの取り違えやローカル破損は確認されなかった。

[CTranslate2 の公式 NLLB 手順](https://opennmt.net/CTranslate2/guides/transformers.html#nllb) と現実装は、入力の encode、token 変換、翻訳先の `target_prefix`、先頭の翻訳先言語 token の除去、decode が一致する。
製品は beam 幅を設定で明示する。公式例の未指定時は 2 であり、製品の既定は 1。

「こんにちは。」の入力 token は `jpn_Jpan, ▁こ, んに, ち, は, 。, </s>`。
日本語指定は反映されていた。生成 hypothesis 自体に `eng_Latn, ▁Hey, ,, ▁hey, ,, ▁hey, ,, ▁hey, .` が含まれる。
反復は decode 後の加工で発生していない。

## 一条件ずつ変えた結果

同じ重みと入力を固定し、beam 幅 1・2・4 を比較した。
[Transformers の NLLB 文書](https://huggingface.co/docs/transformers/v4.51.1/en/model_doc/nllb) にある旧式の言語 token 後置も別途比較した。
`legacy_behaviour=True` の後置では反復回数が変わったが、品質問題は残った。
以下は通常の言語 token 前置による結果。

| 原文 | 言語 | beam 1 | beam 2 | beam 4 |
| --- | --- | --- | --- | --- |
| こんにちは。 | 英語 | Hey, hey, hey, hey. | Hey, how are you? | Hi, how are you? |
| こんにちは。 | 中国語 | 你好,你好吗? | 您好. | 您好. |
| 光合成には日光と水と二酸化炭素が必要です。 | 英語 | Photosynthesis requires sunlight, water and carbon dioxide. | 同左 | 同左 |
| 光合成には日光と水と二酸化炭素が必要です。 | 中国語 | 合成光需要太阳光,水和二氧化碳. | 我们需要太阳光,水和二氧化碳. | 而光合成需要阳光,水和二氧化碳. |
| 教科書の十ページを開いてください。 | 英語 | Open the textbook on pages 10. | Please open ten pages of the textbook. | Please open ten pages of the textbook. |
| 教科書の十ページを開いてください。 | 中国語 | 請打開教科書的十頁. | 請打開一本教科書的十頁. | 请打开教科书的十页. |

beam を増やしても、英語の挨拶には原文にない質問が加わる。
中国語の「光合成」は正しい「光合作用」にならず、beam 2 では「光合成」が脱落して「私たち」が加わる。
教科書のページ指定は beam 2・4 で「10ページ分を開く」という意味へ変わる。
beam 1 の中国語指示は、簡体字の指定に対して繁体字で出力された。
そのため、beam の変更や旧式 token への切替を修正として採用しない。反復を削る後処理も加えない。

この比較で確認できたのは、指定済み int8 モデルと decode 条件での品質制限である。
元の浮動小数点重みとの比較はしておらず、学習済み重みと量子化の寄与は分離していない。
実音声認識、物理マイク、学校 LAN、授業全体の意味精度も、この試験の合格範囲に含めない。

## 疎通と意味精度の別々の判定

`tests/integration/test_real_mt.py::TestNllbEngine` はモデルをロードし、授業文が翻訳されることを一部のキーワードで確認する。
これだけでは挨拶の反復や科学用語の誤訳を検出できない。

`tests/integration/test_nllb_quality.py` は英中の挨拶と光合成の意味を別に判定する。
既知の品質失敗は `xfail(strict=True, raises=AssertionError)` にして、合格件数に混ぜない。
改善して条件を満たした場合は XPASS で失敗し、制限と採否の再評価を要求する。
モデル読み込みなどの別の例外は期待失敗にしない。

```powershell
.venv/Scripts/python.exe -m pytest tests/integration/test_real_mt.py::TestNllbEngine -q
.venv/Scripts/python.exe -m pytest tests/integration/test_nllb_quality.py -q -rx
.venv/Scripts/python.exe -m pytest tests/integration/test_nllb_quality.py --runxfail -q
```

最後のコマンドは、既知の品質不足を実際の失敗として表示する。
今回の実行では、通常モードは 1 passed・3 xfailed、`--runxfail` は 1 passed・3 failed だった。
英語の光合成は合格し、英中の挨拶と中国語の科学用語は訳文の assertion で失敗した。
この小さな検査集合は、翻訳全般の品質保証ではない。
