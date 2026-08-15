#Requires -Version 5.1
<#
Install llama.cpp on native Windows.

winget's ggml.llamacpp package installs the Vulkan x64 build, which runs on
any current GPU vendor. CUDA, ROCm, and CPU-only builds ship as release zips
instead; this script points at them rather than guessing the machine's GPU.
#>
[CmdletBinding()]
param(
    [switch]$Force
)

. (Join-Path $PSScriptRoot "_boot_lib.ps1")

if (-not $Force -and ((Test-CommandPresent "llama-server") -or (Test-CommandPresent "llama"))) {
    Write-Host "llama.cpp already installed:"
    if (Test-CommandPresent "llama-server") { Write-Host "  $((Get-Command llama-server).Source)" }
    if (Test-CommandPresent "llama") { Write-Host "  $((Get-Command llama).Source)" }
    exit 0
}

if (Test-CommandPresent "winget") {
    Write-Host "Installing llama.cpp (Vulkan x64 build) via winget package ggml.llamacpp..."
    winget install --id ggml.llamacpp --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Error "winget install failed. See the manual options below."
    }
    Write-Host "Open a new terminal so PATH picks up the llama.cpp binaries."
}
else {
    Write-Host "winget is not available. Install llama.cpp manually:"
}

Write-Host ""
Write-Host "Manual builds, if you want CUDA/ROCm/CPU instead of Vulkan:"
Write-Host "  https://github.com/ggml-org/llama.cpp/releases"
Write-Host "  Pick llama-<tag>-bin-win-<cpu|cuda-...|vulkan|rocm-...>-x64.zip,"
Write-Host "  unzip somewhere stable, and add that folder to PATH."
