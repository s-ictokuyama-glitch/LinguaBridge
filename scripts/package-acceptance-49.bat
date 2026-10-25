@echo off
rem Issue #49: acceptance check of the distribution package on a PC without Python.
rem Double-click on that PC. Lines above the marker stay ASCII because cmd.exe can
rem misparse non-ASCII text (see start.bat); the rest of this file is PowerShell.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$f='%~f0'; $s=[IO.File]::ReadAllText($f,[Text.Encoding]::UTF8); iex $s.Substring($s.LastIndexOf('#PS-BEGIN'))"
pause
exit /b
#PS-BEGIN
# Python の無い別PCで、配布パッケージの手順書（README の先生向け運用手順書）どおりに
# ダウンロード・展開・起動できるかを案内する。調べられることは自動で調べ、
# 目で見ることだけ候補から番号で選ぶ（期待どおりなら Enter だけ）。
# 記録票はこの bat と同じフォルダに書き出すので、開発機へ持ち帰って docs/ に置く。

$Root = if ($env:LB49_ROOT) { $env:LB49_ROOT } else { 'C:\LinguaBridge' }  # LB49_* は動作確認用
$OutDir = Split-Path -Parent $f
$Today = Get-Date -Format 'yyyy-MM-dd'
$bt = [char]96
$Total = 9
$script:StageNo = 0

function Stage([string]$Title) {
    try { Clear-Host } catch {}
    $script:StageNo++
    Write-Host ""
    Write-Host "▸ ステージ $script:StageNo/$Total · $Title" -ForegroundColor Cyan
}
function Say([string]$t)  { Write-Host "  $t" }
function Step([string]$t) { Write-Host "  • $t" -ForegroundColor White }
function Note([string]$t) { Write-Host "  $t" -ForegroundColor DarkGray }
function Warn([string]$t) { Write-Host "  ⚠ $t" -ForegroundColor Yellow }
function Ok([string]$t)   { Write-Host "  ✓ $t" -ForegroundColor Green }
function Wait([string]$t = '済んだら Enter') { [void](Read-Host "  $t") }

# 候補を番号で選ぶ。Enter は 1（期待どおりの候補）、0 はその他（自分で入力）。
# -Judged の候補は「判定:文言」で、選ぶと判定（#43 と同じ5つ）も決まる。
function Pick([string]$Prompt, [string[]]$Options, [switch]$Judged) {
    Write-Host ""
    Write-Host "  $Prompt" -ForegroundColor White
    for ($k = 0; $k -lt $Options.Count; $k++) {
        $label = $Options[$k]
        if ($Judged) { $p = $label.Split(':', 2); $label = "$($p[1])（$($p[0])）" }
        Write-Host ("    {0}) {1}" -f ($k + 1), $label)
    }
    Write-Host "    0) その他（自分で入力）"
    while ($true) {
        $in = Read-Host "  番号 [Enter で 1]"
        if (-not $in) { $in = '1' }
        if ($in -match '^\d+$' -and [int]$in -le $Options.Count) { break }
        Warn "0〜$($Options.Count) の番号で答えてください"
    }
    if ($in -eq '0') {
        $text = Read-Host "  内容を入力"
        if (-not $text) { $text = 'なし' }
        if (-not $Judged) { return @{ J = $null; N = $text } }
        $j = Pick '判定は？' @('成功', '失敗', '未確認', '根拠付き非該当', '合意により省略')
        return @{ J = $j.N; N = $text }
    }
    $o = $Options[[int]$in - 1]
    if ($Judged) { $p = $o.Split(':', 2); return @{ J = $p[0]; N = $p[1] } }
    return @{ J = $null; N = $o }
}

function Has-Motw([string]$Path) {
    [bool](Get-Item -LiteralPath $Path -Stream Zone.Identifier -ErrorAction SilentlyContinue)
}

# where.exe で見つかったものを実際に動かし、本物の Python かを確かめる。
# WindowsApps の python.exe は Microsoft Store へ案内するだけのスタブで、動かしても失敗する。
function Find-Python([string]$Name) {
    $found = @(& where.exe $Name 2>$null)
    if ($LASTEXITCODE -ne 0) { return @{ Text = '見つからない'; Real = $false } }
    $real = $false
    $lines = foreach ($path in $found) {
        $null = & $path -c "print(1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $real = $true; "$path（動く）" } else { "$path（動かないスタブ）" }
    }
    @{ Text = ($lines -join '、'); Real = $real }
}

Write-Host ""
Write-Host "  Issue #49: Python の無い別PCで配布パッケージを受け入れ確認" -ForegroundColor Cyan
Note "  全 $Total ステージ。画面の指示どおりに操作し、見たものを番号で選びます。"
Note "  期待どおりなら Enter を押すだけです。記録票はこの bat と同じフォルダにできます。"
Wait '始めるには Enter'

# ── 1 ──────────────────────────────────────────────────────────────────
Stage 'このPCの情報と Python が無いことを調べる（自動）'
$cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$winName = if ([int]$cv.CurrentBuild -ge 22000) { 'Windows 11' } else { 'Windows 10 以前' }
$Winver = "$winName $($cv.DisplayVersion)（ビルド $($cv.CurrentBuild).$($cv.UBR)）"
Say "Windows: $Winver"
$py = Find-Python 'python'
$pyl = Find-Python 'py'
Say "where python: $($py.Text)"
Say "where py: $($pyl.Text)"
if ($py.Real -or $pyl.Real) {
    $J_NoPython = '失敗'; Warn '動く Python が見つかりました。このPCは「Python の無いPC」の条件を満たしません。'
} else {
    $J_NoPython = '成功'; Ok '動く Python はありません。'
}
$N_NoPython = "where python: $($py.Text) ／ where py: $($pyl.Text)"
Wait 'Enter で次へ'

# ── 2 ──────────────────────────────────────────────────────────────────
Stage 'zip をダウンロードし、展開の前にブロックを解除する'
Step 'ブラウザで共有リンクを開き、LinguaBridge-<日時>-<英数字>.zip をダウンロードします。'
Wait 'ダウンロードが終わったら Enter'
$downloads = if ($env:LB49_DOWNLOADS) { $env:LB49_DOWNLOADS } else {
    try { (New-Object -ComObject Shell.Application).NameSpace('shell:Downloads').Self.Path } catch { "$env:USERPROFILE\Downloads" }
}
$zip = Get-ChildItem -LiteralPath $downloads -Filter 'LinguaBridge-*.zip' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
while (-not $zip) {
    Warn "ダウンロード フォルダ（$downloads）に LinguaBridge-*.zip が見つかりません。"
    $path = (Read-Host '  zip をこの画面にドラッグ＆ドロップして Enter').Trim('"', ' ')
    if ($path -and (Test-Path -LiteralPath $path)) { $zip = Get-Item -LiteralPath $path }
}
$ZipName = $zip.Name
Say "zip: $ZipName（$([math]::Round($zip.Length / 1GB, 2)) GB）"
$motwBefore = Has-Motw $zip.FullName
if ($motwBefore) { Ok 'ダウンロードした zip にブロックの印（MOTW）が付いています（期待どおり）。' }
else { Warn 'zip にブロックの印（MOTW）が付いていません。ブロックの解除の欄は出ない見込みです。' }
Step 'zip を右クリック →「プロパティ」→「全般」タブを開きます。'
Step 'いちばん下の「セキュリティ」で「ブロックの解除」（「許可する」の場合あり）にチェックを入れて「OK」。'
Warn 'まだ展開しないでください。'
while ($true) {
    Wait 'ブロックの解除が済んだら Enter（欄が無ければそのまま Enter）'
    if (-not $motwBefore) {
        $J_Unblock = '根拠付き非該当'; $N_Unblock = 'ダウンロード直後から印（MOTW）が無く、ブロックの解除の欄も無い'; break
    }
    if (-not (Has-Motw $zip.FullName)) {
        $J_Unblock = '成功'; $N_Unblock = '「ブロックの解除」で印（MOTW）が外れたことを確認'; Ok '印が外れました。'; break
    }
    Warn 'まだ印が付いています。'
    $r = Pick 'どうしますか？' @('もう一度ブロックを解除して調べ直す', 'このまま記録する（失敗）')
    if ($r.N -ne 'もう一度ブロックを解除して調べ直す') {
        $J_Unblock = '失敗'; $N_Unblock = 'ブロックの解除の後も印（MOTW）が残った'; break
    }
}

# ── 3 ──────────────────────────────────────────────────────────────────
Stage "$Root\ に展開する"
if (Test-Path -LiteralPath (Join-Path $Root 'app')) {
    Warn "$Root\app はもうあります。前に展開したものなら、フォルダごと消してから展開してください。"
}
Step "zip を右クリック →「すべて展開」→ 展開先に  $Root  と入力して「展開」。"
Note '  2GB 以上あるので数分かかります。'
while ($true) {
    Wait '展開が終わったら Enter'
    $startBat = Join-Path $Root 'start.bat'
    $bundledPy = Join-Path $Root 'app\python\python.exe'
    if ((Test-Path -LiteralPath $startBat) -and (Test-Path -LiteralPath $bundledPy)) {
        $J_Extract = '成功'; $N_Extract = "直下に start.bat と app\ ができた"
        if (Has-Motw $startBat) { $N_Extract += '（start.bat に印が残っている）'; Warn 'start.bat に印が残っています（初回の起動で外れる見込み）。' }
        Ok "$Root\start.bat と app\ を確認しました。"
        break
    }
    $nested = Get-ChildItem -LiteralPath $Root -Directory -Filter 'LinguaBridge-*' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($nested) { Warn "$($nested.FullName) のように一段深く展開されています。" }
    else { Warn "$Root\start.bat または $Root\app\python\python.exe が見つかりません。" }
    $r = Pick 'どうしますか？' @('展開し直して調べ直す', 'このまま記録する（失敗）')
    if ($r.N -ne '展開し直して調べ直す') {
        $J_Extract = '失敗'
        $N_Extract = if ($nested) { "一段深く展開された（$($nested.Name)）" } else { 'start.bat または app\python\python.exe が無い' }
        break
    }
}

# ── 4 ──────────────────────────────────────────────────────────────────
Stage '初回の start.bat（UAC）'
Step "エクスプローラーで $Root\start.bat をダブルクリックします。"
Note '  期待: SmartScreen も OneDrive の注意も出ず、黄色の「初回の準備を行います」→ UAC で「はい」'
Note '        → 緑の「初回の準備が完了しました」→ 参加コードが出て先生ページが自動で開く'
Warn '黒いウィンドウは閉じないでください。'
$smart = Pick '青い SmartScreen「Windows によって PC が保護されました」は？' @('出なかった', '出た →「詳細情報」→「実行」で進んだ')
$onedrive = Pick '黄色の「OneDrive の同期フォルダの中に置かれています」は？' @('出なかった', '出た')
$uac = Pick 'UAC と初回の準備は？' -Judged @(
    '成功:UAC が1回出て「はい」→ 緑の完了表示 → サーバーが起動した',
    '失敗:UAC が出なかった',
    '失敗:「はい」の後、完了表示ではなく黄色の［注意］が出た',
    '失敗:サーバーが起動しなかった')
$J_FirstRun = $uac.J
$N_FirstRun = "$($uac.N) ／ SmartScreen: $($smart.N) ／ OneDrive の注意: $($onedrive.N)"
# 画面で見たことを、data に残ったもので裏づける
$marker = Join-Path $Root 'data\.first-run-complete'
$log = Join-Path $Root 'data\first-run.log'
if (Test-Path -LiteralPath $marker) { Ok '完了マーカー data\.first-run-complete があります。'; $N_FirstRun += ' ／ 完了マーカー: あり' }
else {
    Warn '完了マーカー data\.first-run-complete がありません。'
    $N_FirstRun += ' ／ 完了マーカー: なし'
    if ($J_FirstRun -eq '成功') { $J_FirstRun = '失敗' }
}
$errors = @(Select-String -LiteralPath $log -SimpleMatch '[エラー]' -ErrorAction SilentlyContinue | ForEach-Object { $_.Line.Trim() })
if (-not (Test-Path -LiteralPath $log)) { Warn 'data\first-run.log がありません。'; $N_FirstRun += ' ／ first-run.log: なし' }
elseif ($errors.Count) {
    Warn "first-run.log に [エラー] が $($errors.Count) 行あります。"; $errors | ForEach-Object { Note "    $_" }
    $N_FirstRun += " ／ first-run.log の [エラー]: $($errors -join '；')"
    if ($J_FirstRun -eq '成功') { $J_FirstRun = '失敗' }
} else { Ok 'first-run.log に [エラー] はありません。'; $N_FirstRun += ' ／ first-run.log の [エラー]: なし' }
$version = try { Get-Content -LiteralPath (Join-Path $Root 'app\version.json') -Raw | ConvertFrom-Json } catch { $null }
Wait 'Enter で次へ'

# ── 5 ──────────────────────────────────────────────────────────────────
Stage '先生ページが HTTPS で開くか'
Say '自動で開いた先生ページのアドレスが  https://<IP>:8443/teacher  であることを確かめます。'
Step '証明書の警告が出たら、起動画面の IP と同じか確認して「詳細設定」→ その IP へ進みます。'
Step 'マイクの許可を求められたら「許可」します。'
$teacher = Pick '先生ページは？' -Judged @(
    '成功:HTTPS で開き、警告を承認してマイクを許可できた',
    '失敗:自動で開かなかった',
    '失敗:証明書の警告から先へ進めなかった',
    '失敗:マイクを許可できなかった')

# ── 6 ──────────────────────────────────────────────────────────────────
Stage '別端末: 生徒ページに字幕が届くか'
Step '生徒用の端末をこのPCと同じ Wi-Fi につなぎます。'
Step '先生ページの QR を読むか、生徒用URL＋4桁の参加コードで参加し、言語を選びます。'
Step '先生ページで「開始」を押し、日本語で数文話します。'
Warn '参加コードは記録しません。'
$device = Pick '生徒端末とブラウザは？' @('iPhone Chrome', 'iPhone Safari', 'Android Chrome', 'Chromebook Chrome')
$lang = Pick '選んだ言語は？' @('英語', '中国語（簡体字）')
$caption = Pick '字幕は？' -Judged @(
    '成功:翻訳された字幕が届いた',
    '失敗:生徒ページを開けなかった・参加できなかった',
    '失敗:参加できたが字幕が出なかった')
$delay = Pick '字幕の遅れは？' @('気にならない', '数秒遅れるが使える', '遅すぎて使えない')

# ── 7 ──────────────────────────────────────────────────────────────────
Stage '2回目の起動で UAC が出ないか'
Step 'サーバーの黒いウィンドウを閉じて止めます（Ctrl+C でも可）。'
Step "もう一度 $Root\start.bat をダブルクリックします。"
$second = Pick '2回目の起動は？' -Judged @(
    '成功:UAC も SmartScreen も出ずに起動した',
    '失敗:UAC がまた出た',
    '失敗:起動しなかった')
Note '  確認が済んだらサーバーのウィンドウを閉じて構いません。'

# ── 8 ──────────────────────────────────────────────────────────────────
Stage '詰まった箇所（#48 の手順書への戻し）'
Say '手順書どおりに進めなかったところ、迷ったところはありましたか？'
Note '  記録票の「#48 に戻す点」に入ります。開発機に持ち帰ってから #48 へ反映します。'
$stuck = Pick '詰まった箇所は？（あれば 0 で内容を入力）' @('なし')

# ── 9 ──────────────────────────────────────────────────────────────────
Stage '記録票を書き出す'
$judges = @($J_NoPython, $J_Unblock, $J_Extract, $J_FirstRun, $teacher.J, $caption.J, $second.J)
$overall = if (@($judges | Where-Object { $_ -ne '成功' }).Count) { '未達（成功でない項目あり）' } else { '成功' }
$build = if ($version) { "commit $bt$($version.commit)$bt、dirty: $($version.dirty)、ビルド: $($version.built_at)" } else { 'app\version.json を読めなかった' }
$record = Join-Path $OutDir "package-acceptance-49-$Today.md"
if (Test-Path -LiteralPath $record) { $record = Join-Path $OutDir "package-acceptance-49-$Today-$(Get-Date -Format HHmm).md" }
$md = @"
# Python の無い別PCでの配布パッケージ受け入れ確認（#49）

仕様: [Issue #49](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/49)（親 #44）。
[Issue #16](https://github.com/s-ictokuyama-glitch/LinguaBridge/issues/16) の受け入れ条件1
（まっさらな Windows 11 機で配布パッケージの展開＋起動バッチだけで運用可能になる）の判断材料。

- 実施日: $Today
- 配布パッケージ: $bt$ZipName$bt（$build）
- 受け渡し: クラウドの共有リンクからブラウザでダウンロード
- 別PC: $Winver
- 総合: **$overall**

判定は 成功／失敗／未確認／根拠付き非該当／合意により省略 の5つ。参加コードは記録しない。
Python・Windows の版・ブロックの印・展開先・完了マーカー・first-run.log は記録用の bat が自動で調べた。

## 結果

| # | 受け入れ条件 | 判定 | 記録 |
|---|---|---|---|
| 1 | 別PCで ${bt}where python${bt} が該当なし | $J_NoPython | $N_NoPython |
| 2a | 展開前のブロックの解除 | $J_Unblock | $N_Unblock |
| 2b | $bt$Root\$bt への展開 | $J_Extract | $N_Extract |
| 2c | 初回の ${bt}start.bat${bt} と UAC の結果 | $J_FirstRun | $N_FirstRun |
| 3a | 先生ページが HTTPS で開く | $($teacher.J) | $($teacher.N) |
| 3b | 別端末の生徒ページに字幕が届く | $($caption.J) | $($caption.N) ／ $($device.N)・$($lang.N) ／ 遅れ: $($delay.N) |
| 4 | 2回目の起動で UAC が出ない | $($second.J) | $($second.N) |

## #48 に戻す点（手順書の修正）

$($stuck.N)

## #16 受け入れ条件1への所見

- 上の 1〜4 がすべて「成功」なら、開発環境の無い Windows 11 機で、配布パッケージの展開と ${bt}start.bat${bt} だけで
  運用可能な状態になったと判断する材料になる。
- 「成功」でない項目は、原因と対処（手順書の修正なら #48、コードの修正なら新しい issue）を決めてから再確認する。
"@
[IO.File]::WriteAllText($record, $md.Replace("`r`n", "`n"), (New-Object Text.UTF8Encoding $false))
Ok "書き出しました: $record"
Say "総合: $overall"
Note '  この記録票を開発機に持ち帰り、docs\ に置いてください。#48 に戻す点があれば開発機から反映します。'
if (-not $env:LB49_ROOT) { Start-Process notepad.exe -ArgumentList "`"$record`"" }
