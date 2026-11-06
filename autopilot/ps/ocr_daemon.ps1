# ocr_daemon.ps1 -- long-lived Windows.Media.Ocr worker.
#
# Why: ocr.ps1 starts a fresh powershell.exe per call (~450ms of the 0.8s).
# Keeping the engine + WinRT bindings warm cuts a read to the engine cost only.
# Protocol: one JSON request per line on stdin, one JSON result per line on stdout.
#   ->  {"path": "C:\\...\\shot.png", "lang": "zh-Hans-CN"}
#   <-  {"ok": true, "engine": "zh-Hans-CN", "ms": 98, "text": "...", "lines": [...]}
# Send "quit" (or close stdin) to stop.
#
# NOTE: ASCII-only on purpose (PowerShell 5.1 reads BOM-less .ps1 as ANSI).

$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8

Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq "AsTask" -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq "IAsyncOperation``1"
    })[0]

function Await($op, $resultType) {
    $m = $asTaskGeneric.MakeGenericMethod($resultType)
    $task = $m.Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    return $task.Result
}

[Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.FileAccessMode, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null

$engines = @{}

function Get-Engine($lang) {
    if ($engines.ContainsKey($lang)) { return $engines[$lang] }
    $eng = $null
    if ($lang -ne "") {
        try {
            $eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language $lang))
        } catch { $eng = $null }
    }
    if ($eng -eq $null) { $eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
    $engines[$lang] = $eng
    return $eng
}

function Send-Line($obj) {
    [Console]::Out.WriteLine(($obj | ConvertTo-Json -Depth 6 -Compress))
    [Console]::Out.Flush()
}

# Warm the default engine once so the first real request is already fast.
try { [void](Get-Engine "") } catch { }
Send-Line @{ ok = $true; ready = $true; pid = $PID }

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line -eq "") { continue }
    if ($line -eq "quit") { break }

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $stream = $null
    try {
        $req = $line | ConvertFrom-Json
        $imgPath = [string]$req.path
        $lang = ""
        if ($req.PSObject.Properties.Name -contains "lang" -and $req.lang) { $lang = [string]$req.lang }
        if (-not (Test-Path -LiteralPath $imgPath)) { throw "image not found: $imgPath" }

        $engine = Get-Engine $lang
        if ($null -eq $engine) { throw "no OCR engine available" }

        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($imgPath)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

        $lines = New-Object System.Collections.ArrayList
        $full = New-Object System.Text.StringBuilder
        foreach ($l in $result.Lines) {
            $minX = [double]::MaxValue; $minY = [double]::MaxValue
            $maxX = [double]::MinValue; $maxY = [double]::MinValue
            foreach ($word in $l.Words) {
                $wr = $word.BoundingRect
                if ($wr.X -lt $minX) { $minX = $wr.X }
                if ($wr.Y -lt $minY) { $minY = $wr.Y }
                if (($wr.X + $wr.Width) -gt $maxX) { $maxX = $wr.X + $wr.Width }
                if (($wr.Y + $wr.Height) -gt $maxY) { $maxY = $wr.Y + $wr.Height }
            }
            if ($minX -eq [double]::MaxValue) { $minX = 0; $minY = 0; $maxX = 0; $maxY = 0 }
            [void]$lines.Add([pscustomobject]@{
                    text = [string]$l.Text
                    rect = @([int]$minX, [int]$minY, [int]($maxX - $minX), [int]($maxY - $minY))
                })
            [void]$full.AppendLine($l.Text)
        }

        $sw.Stop()
        Send-Line @{
            ok     = $true
            engine = $engine.RecognizerLanguage.LanguageTag
            ms     = [int]$sw.ElapsedMilliseconds
            text   = $full.ToString().TrimEnd()
            lines  = @($lines)
        }
    }
    catch {
        $sw.Stop()
        Send-Line @{ ok = $false; ms = [int]$sw.ElapsedMilliseconds; error = $_.Exception.Message }
    }
    finally {
        if ($null -ne $stream) { try { $stream.Dispose() } catch { } }
    }
}
