#Requires -Version 5.1
<#
The Windows boot sequence: fetch the AI stack natively, then hand the
orchestration runtime to WSL2.

Native Windows gets everything that benefits from running on the host: the
llama.cpp runtime (GPU access), the model weights, and the two subscription
sign-ins. The orchestration runtime itself (bash scripts, resident daemons)
targets macOS and Linux, so the last stage points at WSL2 rather than
pretending otherwise.

Every stage is its own script and is idempotent, so this orchestrator is
re-runnable and any stage can be run alone.
#>
[CmdletBinding()]
param(
    [switch]$WithGlimmer,
    [switch]$SkipModels,
    [switch]$SkipLogins,
    [switch]$ForceModels
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Stage {
    param([string]$Name)
    Write-Host ""
    Write-Host "== boot $Name ==" -ForegroundColor White
}

Write-Stage "10-check-prereqs"
& (Join-Path $PSScriptRoot "10-check-prereqs.ps1")

Write-Stage "20-install-llama-cpp"
& (Join-Path $PSScriptRoot "20-install-llama-cpp.ps1")

if (-not $SkipModels) {
    Write-Stage "30-fetch-model-qwen3"
    & (Join-Path $PSScriptRoot "30-fetch-model-qwen3.ps1") -Force:$ForceModels
    Write-Stage "31-fetch-model-gemma4"
    & (Join-Path $PSScriptRoot "31-fetch-model-gemma4.ps1") -Force:$ForceModels
    if ($WithGlimmer) {
        Write-Stage "32-fetch-model-muse-glimmer"
        & (Join-Path $PSScriptRoot "32-fetch-model-muse-glimmer.ps1") -Force:$ForceModels
    }
}
else {
    Write-Host "Skipping model downloads (-SkipModels)."
}

if (-not $SkipLogins) {
    Write-Stage "40-login-anthropic"
    & (Join-Path $PSScriptRoot "40-login-anthropic.ps1") -Install
    Write-Stage "41-login-chatgpt"
    & (Join-Path $PSScriptRoot "41-login-chatgpt.ps1") -Install
}
else {
    Write-Host "Skipping subscription sign-ins (-SkipLogins)."
}

Write-Stage "50-set-default-stack"
& (Join-Path $PSScriptRoot "50-set-default-stack.ps1")

Write-Stage "60-verify-boot"
& (Join-Path $PSScriptRoot "60-verify-boot.ps1")
