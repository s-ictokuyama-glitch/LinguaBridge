# speech.cpp で Haas-ja を評価する

授業評価では `asr.engine: speech-cpp` を指定すると、CPU 上の speech.cpp worker が各 Segment を書き起こす。
Q8_0 と Q4_K の切り替えには、渡す GGUF ファイルを変える。バイナリとモデルは評価環境に置く。

## 取得物

Windows x64 版の speech.cpp 0.10.1 を使った。配布 ZIP の SHA256 は公式の公開値と照合した。[v0.10.1 の配布物](https://github.com/nyosegawa/speech.cpp/releases/expanded_assets/v0.10.1)

GGUF は `sakasegawa/Haas-ja-v1.0-GGUF` のリビジョン `6b965d5007ba780585de51549d57a8c1c0d67c7e` から取得し、SHA256 を公開値と照合した。[固定リビジョンのモデルカード](https://huggingface.co/sakasegawa/Haas-ja-v1.0-GGUF/blob/6b965d5007ba780585de51549d57a8c1c0d67c7e/README.md)

| ファイル | サイズ（バイト） | SHA256 |
| --- | ---: | --- |
| `speech-0.10.1-windows-x64-vulkan.zip` | 29,548,632 | `a8fe0e4b4f463c460e7ace9d38c363bec08b254cac5a7f764b7967af35d4fa1a` |
| `speech.exe` | 46,981,632 | `46f264e26fcaa60f433455c6c6092137c123af67fad956d67f250daf5ebaa893` |
| `Haas-616M-ja-v1.0-Q8_0.gguf` | 656,381,472 | `cd5f60b2f1c411404b83fca4a65e3ce30a26d9e951b6a346529e65f22758afeb` |
| `Haas-616M-ja-v1.0-Q4_K.gguf` | 351,901,216 | `d24d53f29b75214a933ff482b0de1217e0a800f4118cca39ad6cf03609a89d6f` |

## 授業動画を書き起こす

リポジトリ外に評価ディレクトリを作る。配布 ZIP はその中の `speech` フォルダーに展開する。GGUF と次の `speech-cpp.yaml` は評価ディレクトリに置く。

```yaml
asr:
  engine: speech-cpp
  cpu_threads: 4
```

リポジトリのルートから実行する。書き起こしは教材の複製になるため、出力先もリポジトリ外にする。

```powershell
$evalDir = 'C:\asr-eval'
foreach ($quant in 'Q8_0', 'Q4_K') {
    python scripts/lesson_asr_eval.py TestData/testdata.mp4 `
        --config-override "$evalDir/speech-cpp.yaml" `
        --speech-cpp-binary "$evalDir/speech/speech.exe" `
        --speech-cpp-model "$evalDir/Haas-616M-ja-v1.0-$quant.gguf" `
        --candidate-out "$evalDir/speech-cpp-$quant.lesson.json"
}
```

Windows の開発機でこの手順を実行した。Q8_0、Q4_K ともに、50 Segment すべてで文字列を保存した。保存した JSON の入力 SHA256 と各 Segment の開始・終了サンプルは両モデルで一致した。

## カスタム語彙を確かめる

`--speech-cpp-keywords` には、UTF-8 の語彙ファイルを指定する。1 行に 1 語を書く。表記と読みが違う語は `表記=読み` と書く。[speech.cpp 0.10.1 のカスタム語彙仕様](https://github.com/nyosegawa/speech.cpp/blob/v0.10.1/docs/models/fastconformer.md#a-custom-vocabulary-haas)

既知の音声 `tests/fixtures/ja/03.wav` を使った。Q8_0、Q4_K ともに、語彙なしでは「教科書の42ページを開いてください」と出力した。`教科図書=教科書` を与えると、両方とも「教科図書の42ページを開いてください」に変わった。

同じ音声で統合テストを実行できる。バイナリ、GGUF、音声フィクスチャのいずれかが無ければ、該当するテストはスキップする。

```powershell
$env:LINGUABRIDGE_SPEECH_CPP_BINARY = "$evalDir/speech/speech.exe"
$env:LINGUABRIDGE_SPEECH_CPP_MODEL_DIR = $evalDir
python -m pytest -q tests/integration/test_speech_cpp.py
```
