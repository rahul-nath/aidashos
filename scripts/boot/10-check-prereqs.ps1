#Requires -Version 5.1
<#
Read-only readiness report for the Windows boot sequence.
#>
[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot "_boot_lib.ps1")

Write-Host "System dependencies:"
foreach ($name in @("git", "uv", "uvx", "node", "npm", "docker", "llama-server", "codex", "claude")) {
    if (Test-CommandPresent $name) {
        $path = (Get-Command $name).Source
        Write-Host ("  ok      {0}: {1}" -f $name, $path)
    }
    else {
        Write-Host ("  missing {0}" -f $name)
    }
}
if (-not (Test-CommandPresent "llama-server") -and (Test-CommandPresent "llama")) {
    Write-Host "  note    unified 'llama' launcher found; 'llama serve' replaces llama-server"
}

$homeDrive = (Get-Item $HOME).PSDrive
$freeGb = [math]::Floor($homeDrive.Free / 1GB)
Write-Host ""
Write-Host "Disk free on $($homeDrive.Name): $freeGb GB"

# Sized from the pins rather than stated, for the reason the Unix twin gives:
# a written-down size does not follow the quant it describes.
$modelsDir = Get-ModelsDir
$stillNeeded = @()
foreach ($name in @("gemma4", "qwen38")) {
    $pin = Get-ModelPin -Name $name
    $model = Join-Path (Join-Path $modelsDir $pin.Dir) "model.gguf"
    if (-not (Test-Path $model) -or (Get-Item $model).Length -eq 0) { $stillNeeded += $name }
}
if ($stillNeeded.Count -eq 0) {
    Write-Host "Default models are already downloaded; no further space needed for them."
}
else {
    $bytes = 0
    $reachable = $true
    foreach ($name in $stillNeeded) {
        $pin = Get-ModelPin -Name $name
        try {
            $meta = Invoke-RestMethod -TimeoutSec 30 -Uri "https://huggingface.co/api/models/$($pin.Repo)?blobs=true"
        } catch { $reachable = $false; break }
        foreach ($remote in $pin.Files.Keys) {
            $sibling = $meta.siblings | Where-Object { $_.rfilename -eq $remote }
            if ($sibling) { $bytes += [int64]$sibling.size }
        }
    }
    if ($reachable -and $bytes -gt 0) {
        $needGb = [math]::Round($bytes / 1e9)
        Write-Host "Still to download ($($stillNeeded -join ', ')): $needGb GB, measured from the pinned files."
        if ($freeGb -lt $needGb) { Write-Host "warning: not enough free space for that." }
    }
    else {
        Write-Host "Could not reach Hugging Face to size the pinned models; skipping the disk comparison."
    }
}

$ramGb = [math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
Write-Host "Physical memory: $ramGb GB (qwen3.8-27b wants about 20 GB resident)"
if ($ramGb -lt 24) {
    Write-Host "warning: below 24 GB, use gemma4 only and skip the 27B/30B models."
}

Write-Host ""
Write-Host "The orchestration runtime itself runs under WSL2 on Windows; see 60-verify-boot.ps1."

$blocked = $false
foreach ($core in @("git")) {
    if (-not (Test-CommandPresent $core)) {
        Write-Error "blocked: $core is missing."
        $blocked = $true
    }
}
if ($blocked) { exit 1 }
