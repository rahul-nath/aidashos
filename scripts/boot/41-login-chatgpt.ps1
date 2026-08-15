#Requires -Version 5.1
<#
Sign in to the ChatGPT subscription through the Codex CLI.
`codex login` drives a browser flow and returns when it finishes.
#>
[CmdletBinding()]
param(
    [switch]$Install
)

. (Join-Path $PSScriptRoot "_boot_lib.ps1")

if (-not (Test-CommandPresent "codex")) {
    if ($Install -and (Test-CommandPresent "npm")) {
        Write-Host "Installing the Codex CLI..."
        npm install --global @openai/codex
        if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
    }
    else {
        Write-Error "Codex CLI missing. Install with: npm install --global @openai/codex"
        exit 1
    }
}

codex --version
codex login status
if ($LASTEXITCODE -eq 0) {
    Write-Host "Codex is already signed in."
    exit 0
}

Write-Host "Opening the Codex sign-in (browser flow)..."
codex login
