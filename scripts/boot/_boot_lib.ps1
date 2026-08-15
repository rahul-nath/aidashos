# Shared helpers and model pins for the Windows boot scripts.
#
# The canonical model pin table is scripts/download-models.sh; this file must
# stay in step with it. It exists because the .sh cannot run on native Windows
# and three fetch scripts should not each carry their own copy of the pins.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:BootRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

function Get-AgentOsRoot {
    return $script:BootRoot
}

function Test-CommandPresent {
    param([Parameter(Mandatory = $true)][string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Get-ModelsDir {
    if ($env:LOCAL_AGENT_LLAMA_MODELS_DIR) {
        return $env:LOCAL_AGENT_LLAMA_MODELS_DIR
    }
    return (Join-Path $HOME "models")
}

# name -> repo, files (hf file name -> local name), note
$script:ModelPins = @{
    # The one pin whose two files come from two repositories. Unsloth ships the
    # UD-Q5_K_XL target; it publishes no MTP drafter, so the draft still comes
    # from ggml-org. `Repo` is the default and `FileRepos` overrides it per file.
    "qwen38" = @{
        Repo  = "unsloth/Qwen3.8-27B-GGUF"
        Dir   = "qwen3.8-27b-mtp"
        Files = [ordered]@{
            "Qwen3.8-27B-UD-Q5_K_XL.gguf" = "model.gguf"
            "mtp-Qwen3.8-27B-Q4_0.gguf"   = "draft.gguf"
        }
        FileRepos = @{
            "mtp-Qwen3.8-27B-Q4_0.gguf" = "ggml-org/Qwen3.8-27B-GGUF"
        }
        Note  = "default heavyweight local model, about 22 GB with the MTP draft"
    }
    "gemma4" = @{
        Repo  = "unsloth/gemma-4-E4B-it-GGUF"
        Dir   = "gemma4"
        Files = [ordered]@{
            "gemma-4-E4B-it-Q4_K_M.gguf" = "model.gguf"
            "mmproj-F16.gguf"            = "mmproj.gguf"
        }
        Note  = "junior tier model, about 5 GB with the projector"
    }
    "glimmer" = @{
        Repo  = "meta-models/Muse-Glimmer-30B-GGUF"
        Dir   = "glimmer"
        Files = [ordered]@{
            "muse-glimmer-30B-kquant-dynamic.gguf" = "model.gguf"
            "dflash-kquant.gguf"                   = "draft.gguf"
        }
        Note  = "optional deliberator, about 21 GB with the DFlash draft"
    }
}

function Get-ModelPin {
    param([Parameter(Mandatory = $true)][string]$Name)
    if (-not $script:ModelPins.ContainsKey($Name)) {
        throw "Unknown model pin: $Name"
    }
    return $script:ModelPins[$Name]
}

function Invoke-ModelDownload {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [switch]$Force
    )
    $pin = Get-ModelPin -Name $Name
    $dir = Join-Path (Get-ModelsDir) $pin.Dir
    New-Item -ItemType Directory -Force -Path $dir | Out-Null

    $allPresent = $true
    foreach ($local in $pin.Files.Values) {
        $path = Join-Path $dir $local
        if (-not (Test-Path $path) -or (Get-Item $path).Length -eq 0) {
            $allPresent = $false
        }
    }
    if ($allPresent -and -not $Force) {
        Write-Host "$Name already present: $dir"
        return
    }

    Write-Host "Fetching $Name ($($pin.Note)) into $dir"
    foreach ($entry in $pin.Files.GetEnumerator()) {
        $remote = $entry.Key
        $local = Join-Path $dir $entry.Value
        # A pin may draw its files from more than one repository: qwen38 takes
        # its target from Unsloth and its MTP drafter from ggml-org, which is
        # the only place that publishes one.
        $repo = $pin.Repo
        if ($pin.Contains("FileRepos") -and $pin.FileRepos.ContainsKey($remote)) {
            $repo = $pin.FileRepos[$remote]
        }
        if ((Test-Path $local) -and (Get-Item $local).Length -gt 0 -and -not $Force) {
            Write-Host "  present: $($entry.Value)"
            continue
        }
        if (Test-CommandPresent "uvx") {
            # Same downloader the Unix scripts use, resumable and checksummed.
            uvx --from huggingface-hub hf download $repo $remote --local-dir $dir
            if ($LASTEXITCODE -ne 0) { throw "hf download failed for $remote" }
            $downloaded = Join-Path $dir $remote
            if ($downloaded -ne $local) {
                # Hard link keeps both spellings without a 20 GB copy; fall back
                # to a rename when the filesystem refuses.
                if (Test-Path $local) { Remove-Item $local -Force }
                try {
                    New-Item -ItemType HardLink -Path $local -Target $downloaded | Out-Null
                } catch {
                    Move-Item -Path $downloaded -Destination $local -Force
                }
            }
        }
        else {
            # curl.exe ships with Windows 10 1803+ and supports resume.
            $url = "https://huggingface.co/$repo/resolve/main/$remote"
            Write-Host "  uvx not found; downloading with curl.exe from $url"
            curl.exe -fL --retry 3 --continue-at - -o $local $url
            if ($LASTEXITCODE -ne 0) { throw "curl download failed for $remote" }
        }
    }
    Write-Host "Done: $dir"
}
