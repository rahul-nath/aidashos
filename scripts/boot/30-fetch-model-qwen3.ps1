#Requires -Version 5.1
<#
Fetch the default heavyweight local model: Qwen3.8-27B (Q4_K_M + MTP draft).
The qwen3.x default is the 27B; new variants get pinned in
scripts/download-models.sh first, then mirrored in _boot_lib.ps1.
#>
[CmdletBinding()]
param(
    [ValidateSet("27b")]
    [string]$Variant = "27b",
    [switch]$Force
)

. (Join-Path $PSScriptRoot "_boot_lib.ps1")

Invoke-ModelDownload -Name "qwen38" -Force:$Force
