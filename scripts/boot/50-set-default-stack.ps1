#Requires -Version 5.1
<#
Apply the default stack config on Windows and report what it resolves to.

The checked-in configs are the stack; this materializes .env and reports which
registry models are installed. The orchestration runtime consumes these under
WSL2, so the .env written here matters when WSL mounts this same checkout.
#>
[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot "_boot_lib.ps1")
$root = Get-AgentOsRoot

$envPath = Join-Path $root ".env"
if (-not (Test-Path $envPath)) {
    Copy-Item (Join-Path $root ".env.example") $envPath
    Write-Host "Created .env from .env.example."
}
else {
    Write-Host ".env already exists; left unchanged."
}

if (-not (Test-Path (Join-Path $root "configs\linked_projects.toml"))) {
    Write-Error "blocked: configs/linked_projects.toml is missing. Restore it from git."
    exit 1
}

$modelsDir = Get-ModelsDir
Write-Host ""
Write-Host "Local models under ${modelsDir}:"
foreach ($name in @("gemma4", "qwen38", "glimmer")) {
    $pin = Get-ModelPin -Name $name
    $dir = Join-Path $modelsDir $pin.Dir
    $present = $true
    foreach ($local in $pin.Files.Values) {
        $path = Join-Path $dir $local
        if (-not (Test-Path $path) -or (Get-Item $path).Length -eq 0) { $present = $false }
    }
    $marker = if ($present) { "ok     " } else { "missing" }
    Write-Host ("  {0} {1,-10} {2}" -f $marker, $name, $dir)
}

# Same guard the Unix stack writes: two roughly 20GB models must not be
# resident at once until residency scheduling exists.
$qwenPresent = Test-Path (Join-Path $modelsDir "qwen3.8-27b-mtp\model.gguf")
$glimmerPresent = Test-Path (Join-Path $modelsDir "glimmer\model.gguf")
if ($qwenPresent -and $glimmerPresent) {
    $envContent = Get-Content $envPath -Raw
    if ($envContent -match "(?m)^LOCAL_AGENT_LLAMA_MODELS_MAX=") {
        Write-Host ""
        Write-Host "Resident-model cap already set in .env."
    }
    else {
        Add-Content $envPath "`n# Both glimmer and qwen3.8-27b are installed (about 20 GB resident each)."
        Add-Content $envPath "# Cap the llama router at one loaded model until residency scheduling exists."
        Add-Content $envPath "LOCAL_AGENT_LLAMA_MODELS_MAX=1"
        Write-Host ""
        Write-Host "Wrote LOCAL_AGENT_LLAMA_MODELS_MAX=1 to .env (both heavyweight models are installed)."
    }
}

Write-Host ""
Write-Host "Default stack is in place. Next: ./scripts/boot/60-verify-boot.ps1"
