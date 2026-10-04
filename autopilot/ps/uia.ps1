# uia.ps1 -- UI Automation element tree dumper for autopilot.
#
# NOTE: this file is intentionally ASCII-only. Windows PowerShell 5.1 reads
# .ps1 files without a BOM as ANSI, so non-ASCII comments would be mangled.
# All payload data travels through UTF-8 JSON files instead.
#
# Usage:  powershell.exe -NoProfile -ExecutionPolicy Bypass -File uia.ps1 -Job <job.json>
# Job:    { "mode": "tree"|"focused", "hwnd": 12345, "maxDepth": 10, "maxNodes": 400 }
# Result: written to <job>.out.json as UTF-8 (no BOM)

param([Parameter(Mandatory = $true)][string]$Job)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName WindowsBase

$cfg = Get-Content -LiteralPath $Job -Raw -Encoding UTF8 | ConvertFrom-Json

$maxDepth = 10
$maxNodes = 400
$mode = "tree"
$hwnd = 0
if ($cfg.PSObject.Properties.Name -contains "maxDepth" -and $cfg.maxDepth) { $maxDepth = [int]$cfg.maxDepth }
if ($cfg.PSObject.Properties.Name -contains "maxNodes" -and $cfg.maxNodes) { $maxNodes = [int]$cfg.maxNodes }
if ($cfg.PSObject.Properties.Name -contains "mode" -and $cfg.mode) { $mode = [string]$cfg.mode }
if ($cfg.PSObject.Properties.Name -contains "hwnd" -and $cfg.hwnd) { $hwnd = [int64]$cfg.hwnd }

function Write-Result($data) {
    $json = $data | ConvertTo-Json -Depth 8 -Compress
    $path = "$Job.out.json"
    [System.IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function Get-Patterns($el) {
    $list = New-Object System.Collections.ArrayList
    $pairs = @(
        @("Invoke", [System.Windows.Automation.InvokePattern]::Pattern),
        @("Value", [System.Windows.Automation.ValuePattern]::Pattern),
        @("Toggle", [System.Windows.Automation.TogglePattern]::Pattern),
        @("SelectionItem", [System.Windows.Automation.SelectionItemPattern]::Pattern),
        @("ExpandCollapse", [System.Windows.Automation.ExpandCollapsePattern]::Pattern),
        @("Scroll", [System.Windows.Automation.ScrollPattern]::Pattern),
        @("RangeValue", [System.Windows.Automation.RangeValuePattern]::Pattern),
        @("Text", [System.Windows.Automation.TextPattern]::Pattern)
    )
    foreach ($p in $pairs) {
        try {
            $obj = $null
            if ($el.TryGetCurrentPattern($p[1], [ref]$obj)) { [void]$list.Add($p[0]) }
        } catch { }
    }
    return , $list
}

function Get-ValueText($el, $patterns) {
    if ($patterns -notcontains "Value") { return $null }
    try {
        $obj = $null
        if ($el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$obj)) {
            $v = ([System.Windows.Automation.ValuePattern]$obj).Current.Value
            if ($v -ne $null -and $v.Length -gt 300) { return $v.Substring(0, 300) }
            return $v
        }
    } catch { }
    return $null
}

try {
    if ($mode -eq "focused") {
        try {
            $el = [System.Windows.Automation.AutomationElement]::FocusedElement
        } catch { $el = $null }
        if ($el -eq $null) { Write-Result @{ ok = $true; mode = "focused"; focused = $null }; exit 0 }
        $c = $el.Current
        $r = $c.BoundingRectangle
        $pats = Get-Patterns $el
        Write-Result @{
            ok      = $true
            mode    = "focused"
            focused = @{
                name     = $c.Name
                type     = $c.ControlType.ProgrammaticName -replace "ControlType\.", ""
                class    = $c.ClassName
                aid      = $c.AutomationId
                rect     = @([int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height)
                enabled  = [bool]$c.IsEnabled
                patterns = @($pats)
                value    = (Get-ValueText $el $pats)
            }
        }
        exit 0
    }

    if ($hwnd -ne 0) {
        $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwnd)
    }
    else {
        $root = [System.Windows.Automation.AutomationElement]::RootElement
    }
    if ($root -eq $null) { Write-Result @{ ok = $false; error = "no root element" }; exit 0 }

    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $queue = New-Object System.Collections.ArrayList
    [void]$queue.Add([pscustomobject]@{ el = $root; depth = 0; path = "0" })

    $out = New-Object System.Collections.ArrayList
    $i = 0
    $guard = 0
    while ($i -lt $queue.Count -and $out.Count -lt $maxNodes -and $guard -lt ($maxNodes * 6)) {
        $guard++
        $node = $queue[$i]
        $i++
        $el = $node.el
        $depth = $node.depth

        $name = ""
        $type = ""
        $cls = ""
        $aid = ""
        $rx = 0; $ry = 0; $rw = 0; $rh = 0
        $enabled = $false
        $offscreen = $true
        $pats = @()
        $value = $null
        try {
            $c = $el.Current
            $name = [string]$c.Name
            $type = [string]($c.ControlType.ProgrammaticName -replace "ControlType\.", "")
            $cls = [string]$c.ClassName
            $aid = [string]$c.AutomationId
            $r = $c.BoundingRectangle
            $rx = [int]$r.X; $ry = [int]$r.Y; $rw = [int]$r.Width; $rh = [int]$r.Height
            $enabled = [bool]$c.IsEnabled
            $offscreen = [bool]$c.IsOffscreen
            $pats = Get-Patterns $el
            $value = Get-ValueText $el $pats
        } catch {
            # element vanished mid-walk; skip it
            continue
        }

        $show = $true
        if ($offscreen) { $show = $false }
        if ($rw -le 0 -or $rh -le 0) { $show = $false }
        if ([string]::IsNullOrWhiteSpace($name) -and $pats.Count -eq 0 -and [string]::IsNullOrWhiteSpace($aid)) {
            # structural container with no handle for the agent -> only keep if shallow
            if ($depth -gt 2) { $show = $false }
        }

        if ($show) {
            [void]$out.Add([pscustomobject]@{
                    i        = $out.Count
                    path     = $node.path
                    depth    = $depth
                    name     = $name
                    type     = $type
                    class    = $cls
                    aid      = $aid
                    rect     = @($rx, $ry, $rw, $rh)
                    enabled  = $enabled
                    patterns = @($pats)
                    value    = $value
                })
        }

        if ($depth -lt $maxDepth) {
            try {
                $child = $walker.GetFirstChild($el)
                $k = 0
                while ($child -ne $null -and $k -lt 200) {
                    [void]$queue.Add([pscustomobject]@{ el = $child; depth = ($depth + 1); path = ("$($node.path).$k") })
                    $child = $walker.GetNextSibling($child)
                    $k++
                }
            } catch { }
        }
    }

    $fg = [System.Windows.Automation.AutomationElement]::FocusedElement
    $fgInfo = $null
    if ($fg -ne $null) {
        try {
            $fc = $fg.Current
            $fr = $fc.BoundingRectangle
            $fgInfo = @{
                name  = [string]$fc.Name
                type  = [string]($fc.ControlType.ProgrammaticName -replace "ControlType\.", "")
                rect  = @([int]$fr.X, [int]$fr.Y, [int]$fr.Width, [int]$fr.Height)
                value = (Get-ValueText $fg (Get-Patterns $fg))
            }
        } catch { }
    }

    $rootInfo = $null
    try {
        $rc = $root.Current
        $rr = $rc.BoundingRectangle
        $rootInfo = @{
            name = [string]$rc.Name
            type = [string]($rc.ControlType.ProgrammaticName -replace "ControlType\.", "")
            rect = @([int]$rr.X, [int]$rr.Y, [int]$rr.Width, [int]$rr.Height)
        }
    } catch { }

    Write-Result @{
        ok       = $true
        mode     = "tree"
        root     = $rootInfo
        focused  = $fgInfo
        count    = $out.Count
        truncated = [bool]($out.Count -ge $maxNodes)
        elements = @($out)
    }
}
catch {
    Write-Result @{ ok = $false; error = $_.Exception.Message; trace = $_.ScriptStackTrace }
    exit 0
}
