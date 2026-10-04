# toast.ps1 -- Windows toast notification bridge for autopilot.
# ASCII-only on purpose (PowerShell 5.1 reads BOM-less .ps1 as ANSI).
#
# Usage:  powershell.exe -NoProfile -ExecutionPolicy Bypass -File toast.ps1 -Job <job.json>
# Job:    { "title": "...", "message": "...", "appId": "" }
# Result: <job>.out.json

param([Parameter(Mandatory = $true)][string]$Job)

$ErrorActionPreference = "Stop"

function Write-Result($data) {
    $json = $data | ConvertTo-Json -Depth 6 -Compress
    [System.IO.File]::WriteAllText("$Job.out.json", $json, (New-Object System.Text.UTF8Encoding($false)))
}

try {
    $cfg = Get-Content -LiteralPath $Job -Raw -Encoding UTF8 | ConvertFrom-Json
    $title = [string]$cfg.title
    $message = ""
    if ($cfg.PSObject.Properties.Name -contains "message") { $message = [string]$cfg.message }
    $appId = ""
    if ($cfg.PSObject.Properties.Name -contains "appId" -and $cfg.appId) { $appId = [string]$cfg.appId }

    if ($appId -eq "") {
        # AUMID of the built-in Windows PowerShell shortcut; works for unpackaged apps.
        $appId = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
    }

    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $nodes = $template.GetElementsByTagName("text")
    $nodes.Item(0).AppendChild($template.CreateTextNode($title)) | Out-Null
    $nodes.Item(1).AppendChild($template.CreateTextNode($message)) | Out-Null

    $toast = New-Object Windows.UI.Notifications.ToastNotification $template
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)

    Write-Result @{ ok = $true; shown = $true }
}
catch {
    Write-Result @{ ok = $false; error = $_.Exception.Message }
}
exit 0
