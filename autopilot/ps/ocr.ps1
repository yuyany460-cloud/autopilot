# ocr.ps1 -- Windows.Media.Ocr bridge for autopilot.
#
# NOTE: ASCII-only on purpose (PowerShell 5.1 reads BOM-less .ps1 as ANSI).
# This uses the OCR engine that ships with Windows 10/11, so no pip package
# and no external binary is required. Language availability depends on the
# installed language packs; zh-Hans-CN also recognises Latin text.
#
# Usage:  powershell.exe -NoProfile -ExecutionPolicy Bypass -File ocr.ps1 -Job <job.json>
# Job:    { "path": "C:\\...\\shot.png", "lang": "zh-Hans-CN" }
# Result: written to <job>.out.json

param([Parameter(Mandatory = $true)][string]$Job)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8

function Write-Result($data) {
    $json = $data | ConvertTo-Json -Depth 8 -Compress
    $path = "$Job.out.json"
    [System.IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))
}

try {
    $cfg = Get-Content -LiteralPath $Job -Raw -Encoding UTF8 | ConvertFrom-Json
    $imgPath = [string]$cfg.path
    $lang = ""
    if ($cfg.PSObject.Properties.Name -contains "lang" -and $cfg.lang) { $lang = [string]$cfg.lang }

    if (-not (Test-Path -LiteralPath $imgPath)) {
        Write-Result @{ ok = $false; error = "image not found: $imgPath" }
        exit 0
    }

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

    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($imgPath)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

    $engine = $null
    if ($lang -ne "") {
        try {
            $language = New-Object Windows.Globalization.Language $lang
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
        } catch { $engine = $null }
    }
    if ($engine -eq $null) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    }
    if ($engine -eq $null) {
        $avail = @()
        foreach ($l in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) { $avail += $l.LanguageTag }
        Write-Result @{ ok = $false; error = "no OCR engine"; available = $avail }
        exit 0
    }

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $sw.Stop()

    $lines = New-Object System.Collections.ArrayList
    $full = New-Object System.Text.StringBuilder
    foreach ($line in $result.Lines) {
        $minX = [double]::MaxValue; $minY = [double]::MaxValue
        $maxX = [double]::MinValue; $maxY = [double]::MinValue
        foreach ($word in $line.Words) {
            $wr = $word.BoundingRect
            if ($wr.X -lt $minX) { $minX = $wr.X }
            if ($wr.Y -lt $minY) { $minY = $wr.Y }
            if (($wr.X + $wr.Width) -gt $maxX) { $maxX = $wr.X + $wr.Width }
            if (($wr.Y + $wr.Height) -gt $maxY) { $maxY = $wr.Y + $wr.Height }
        }
        if ($minX -eq [double]::MaxValue) { $minX = 0; $minY = 0; $maxX = 0; $maxY = 0 }
        [void]$lines.Add([pscustomobject]@{
                text = [string]$line.Text
                rect = @([int]$minX, [int]$minY, [int]($maxX - $minX), [int]($maxY - $minY))
            })
        [void]$full.AppendLine($line.Text)
    }

    Write-Result @{
        ok      = $true
        engine  = $engine.RecognizerLanguage.LanguageTag
        ms      = [int]$sw.ElapsedMilliseconds
        text    = $full.ToString().TrimEnd()
        lines   = @($lines)
    }
}
catch {
    Write-Result @{ ok = $false; error = $_.Exception.Message; trace = $_.ScriptStackTrace }
    exit 0
}
