#Requires -Version 5.1
<#
Fetch the optional deliberator model: Muse-Glimmer-30B (+ DFlash draft).
Optional because it is a second roughly 20GB-resident model; do not serve it
at the same time as qwen3.8-27b on a 32-36 GB machine.
#>
[CmdletBinding()]
param(
    [switch]$Force
)

. (Join-Path $PSScriptRoot "_boot_lib.ps1")

Invoke-ModelDownload -Name "glimmer" -Force:$Force
Write-Host ""
Write-Host "Note: glimmer and qwen3.8-27b are each about 20 GB resident."
Write-Host "Serve one at a time (the Unix stack writes LOCAL_AGENT_LLAMA_MODELS_MAX=1 for this)."
