# ベンチ・テスト用の日本語音声フィクスチャを Windows SAPI の ja-JP 音声（例: Haruka）で合成する。
#   powershell -ExecutionPolicy Bypass -File scripts\make_fixture_audio.ps1
# 出力: tests\fixtures\ja\NN.wav（16kHz mono PCM16）
# 文リストは tests\fixtures\ja_sentences.txt（1行1文）。
# 注: TTS音声は速度計測（RTF）用。実教室のマイク品質での精度確認は実地検証（#19）。
param(
    [string]$OutDir = (Join-Path $PSScriptRoot "..\tests\fixtures\ja"),
    [string]$SentenceFile = (Join-Path $PSScriptRoot "..\tests\fixtures\ja_sentences.txt")
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

New-Item -ItemType Directory -Force $OutDir | Out-Null
$sentences = Get-Content -Encoding UTF8 $SentenceFile | Where-Object { $_.Trim() -ne "" }

$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)

$i = 0
foreach ($s in $sentences) {
    $i++
    $path = Join-Path $OutDir ("{0:d2}.wav" -f $i)
    $synth.SetOutputToWaveFile($path, $fmt)
    $synth.Speak($s)
    $synth.SetOutputToNull()
    Write-Host ("{0}: {1}" -f (Split-Path -Leaf $path), $s)
}
$synth.Dispose()
Write-Host "done: $i files -> $OutDir"
