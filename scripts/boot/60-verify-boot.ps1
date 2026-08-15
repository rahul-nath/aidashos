#Requires -Version 5.1
<#
Verify the Windows boot and hand off to WSL2 for the runtime.

Native Windows carries the model stack and the subscriptions; the runtime and
its full readiness check (scripts/first-run-check.sh) run inside WSL2.
#>
[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot "_boot_lib.ps1")

$blocked = 0

foreach ($name in @("llama-server", "codex", "claude")) {
    if (Test-CommandPresent $name) {
        Write-Host ("  ok      {0}" -f $name)
    }
    elseif ($name -eq "llama-server" -and (Test-CommandPresent "llama")) {
        Write-Host "  ok      llama (unified launcher)"
    }
    else {
        Write-Host ("  blocked {0}" -f $name)
        $blocked += 1
    }
}

$modelsDir = Get-ModelsDir
foreach ($name in @("gemma4", "qwen38")) {
    $pin = Get-ModelPin -Name $name
    $model = Join-Path (Join-Path $modelsDir $pin.Dir) "model.gguf"
    if ((Test-Path $model) -and (Get-Item $model).Length -gt 0) {
        Write-Host ("  ok      model {0}" -f $name)
    }
    else {
        Write-Host ("  blocked model {0} (run boot stage 3x)" -f $name)
        $blocked += 1
    }
}

Write-Host ""
if ($blocked -eq 0) {
    Write-Host "Native Windows stack is ready."
}
else {
    Write-Host "$blocked item(s) blocked. Re-run .\scripts\boot\boot.ps1; every stage is idempotent."
}

Write-Host ""
Write-Host "The orchestration runtime runs inside WSL2 (Ubuntu):"
Write-Host "  1. wsl --install -d Ubuntu   (once, from an elevated terminal)"
Write-Host "  2. Inside WSL: clone the repo, run make, then ./scripts/boot/boot.sh --skip-models"
Write-Host "  3. Reuse these native model files by setting, in the WSL .env:"
Write-Host "     LOCAL_AGENT_LLAMA_MODELS_DIR=/mnt/c/Users/$env:USERNAME/models"
Write-Host "     (or refetch inside WSL for filesystem speed)"

exit ([int]($blocked -gt 0))
