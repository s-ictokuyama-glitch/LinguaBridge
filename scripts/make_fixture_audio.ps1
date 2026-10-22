# ベンチ・テスト用の日本語音声フィクスチャを Windows SAPI の ja-JP 音声（例: Haruka）で合成する。
#   powershell -ExecutionPolicy Bypass -File scripts\make_fixture_audio.ps1
#
# 生成物は2系統ある:
#   1. ベースライン10文  tests\fixtures\ja\NN.wav（16kHz mono PCM16）
#      文リストは tests\fixtures\ja_sentences.txt（1行1文）。**構成を変えないこと** —
#      scripts\bench.py が glob し、tests\integration\test_real_asr.py が 03.wav を名指しし、
#      docs\bench\2026-07-07-bench.md との比較可能性がここに乗っている。
#   2. 拡張コーパス      tests\fixtures\ja_ext\（イシュー#22）
#      話速・文中の間・言い淀み・雑音のバリエーション。定義は tests\fixtures\ja_corpus.json。
#      ここではセグメント単位に合成するだけで、無音の挿入・連結・雑音の重畳・
#      正解アノテーションの展開は scripts\build_fixture_corpus.py が行う。
#      （SAPI の break は指定どおりの長さにならないので、間は合成後に自前で入れる。
#        そうしてはじめてギャップの時刻が区切りベンチの正解として使える。）
#
# 注: TTS音声は速度計測（RTF）と before/after の差分用。CER の絶対値は実力ではない。
#     実教室のマイク品質での精度確認は実地検証（#19）。
param(
    [string]$OutDir = (Join-Path $PSScriptRoot "..\tests\fixtures\ja"),
    [string]$SentenceFile = (Join-Path $PSScriptRoot "..\tests\fixtures\ja_sentences.txt"),
    [string]$CorpusFile = (Join-Path $PSScriptRoot "..\tests\fixtures\ja_corpus.json"),
    [string]$ExtDir = (Join-Path $PSScriptRoot "..\tests\fixtures\ja_ext"),
    # ベースライン10文だけを作り直す（拡張コーパスに触らない）
    [switch]$BaseOnly
)

Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer

$jaVoice = $synth.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq "ja-JP" } | Select-Object -First 1
if ($null -eq $jaVoice) {
    Write-Error "ja-JP の音声が見つかりません。設定 > 時刻と言語 > 音声 で日本語音声を追加してください。"
    exit 1
}
$synth.SelectVoice($jaVoice.VoiceInfo.Name)
Write-Host "voice: $($jaVoice.VoiceInfo.Name)"

$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)

function Write-Utterance {
    param([string]$Text, [string]$Path, [int]$Rate = 0)
    $synth.Rate = $Rate
    $synth.SetOutputToWaveFile($Path, $fmt)
    $synth.Speak($Text)
    $synth.SetOutputToNull()
}

# ---- 1. ベースライン10文 ----

New-Item -ItemType Directory -Force $OutDir | Out-Null
$sentences = Get-Content -Encoding UTF8 $SentenceFile | Where-Object { $_.Trim() -ne "" }

$i = 0
foreach ($s in $sentences) {
    $i++
    $path = Join-Path $OutDir ("{0:d2}.wav" -f $i)
    Write-Utterance -Text $s -Path $path -Rate 0
    Write-Host ("{0}: {1}" -f (Split-Path -Leaf $path), $s)
}
Write-Host "base: $i files -> $OutDir"

if ($BaseOnly) {
    $synth.Dispose()
    exit 0
}

# ---- 2. 拡張コーパス（セグメント単位で合成 → Python 側で組み立て） ----

if (-not (Test-Path $CorpusFile)) {
    $synth.Dispose()
    Write-Error "コーパス定義が見つかりません: $CorpusFile"
    exit 1
}

$corpus = Get-Content -Raw -Encoding UTF8 $CorpusFile | ConvertFrom-Json
$segmentDir = Join-Path ([System.IO.Path]::GetTempPath()) ("lb_fixture_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $segmentDir | Out-Null

try {
    $segCount = 0
    foreach ($clip in $corpus.clips) {
        # noise / noise_only は既存クリップから Python 側で派生させるので合成しない
        if ($clip.category -eq "noise" -or $clip.category -eq "noise_only") { continue }

        $rate = 0
        if ($null -ne $clip.rate) { $rate = [int]$clip.rate }
        $segments = @($clip.segments)
        for ($j = 0; $j -lt $segments.Count; $j++) {
            $path = Join-Path $segmentDir ("{0}__{1:d2}.wav" -f $clip.id, $j)
            Write-Utterance -Text $segments[$j].text -Path $path -Rate $rate
            $segCount++
        }
        Write-Host ("{0} (rate={1}, {2} seg)" -f $clip.id, $rate, $segments.Count)
    }
    $synth.Dispose()
    Write-Host "segments: $segCount -> $segmentDir"

    $python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { $python = "python" }
    & $python (Join-Path $PSScriptRoot "build_fixture_corpus.py") --segments-dir $segmentDir --out-dir $ExtDir
    if ($LASTEXITCODE -ne 0) {
        Write-Error "build_fixture_corpus.py が失敗しました（終了コード $LASTEXITCODE）"
        exit 1
    }
}
finally {
    Remove-Item -Recurse -Force $segmentDir -ErrorAction SilentlyContinue
}

Write-Host "done"
