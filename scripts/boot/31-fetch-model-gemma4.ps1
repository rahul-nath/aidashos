#Requires -Version 5.1
<#
Fetch the junior-tier model: gemma-4-E4B-it (Q4_K_M + vision projector).
This is the model the system will not run without.
#>
[CmdletBinding()]
param(
    [switch]$Force
)

. (Join-Path $PSScriptRoot "_boot_lib.ps1")

Invoke-ModelDownload -Name "gemma4" -Force:$Force
