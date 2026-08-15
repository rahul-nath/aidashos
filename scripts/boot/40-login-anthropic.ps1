#Requires -Version 5.1
<#
Sign in to the Anthropic subscription through Claude Code.
Sign-in is a browser flow Claude Code drives itself; complete it, then type
/exit to return to the boot sequence.
#>
[CmdletBinding()]
param(
    [switch]$Install
)

. (Join-Path $PSScriptRoot "_boot_lib.ps1")

if (-not (Test-CommandPresent "claude")) {
    if ($Install -and (Test-CommandPresent "npm")) {
        Write-Host "Installing Claude Code..."
        npm install --global @anthropic-ai/claude-code
        if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
    }
    else {
        Write-Error "Claude Code missing. Install with: npm install --global @anthropic-ai/claude-code"
        exit 1
    }
}

claude --version
Write-Host ""
Write-Host "Opening the Claude Code sign-in. Complete it, then type /exit to continue the boot."
claude /login
exit 0
