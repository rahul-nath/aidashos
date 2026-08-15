# The boot sequence

Everything after `make`, in dependency order.
`make` leaves a working uv environment, Docker Postgres, and schemas.
The boot sequence takes the machine the rest of the way: local model runtime, model weights, frontier subscriptions, stack config, and a final verification.

The full picture is drawn in [docs/diagrams/aidashos-onboarding-dag.png](../../docs/diagrams/aidashos-onboarding-dag.png).

## One command

```bash
./scripts/boot/boot.sh
```

Windows (native PowerShell):

```powershell
.\scripts\boot\boot.ps1
```

Or paste [docs/onboarding/BOOT_PROMPT.md](../../docs/onboarding/BOOT_PROMPT.md) into any local AI agent and let it drive the same stages.

## Stages

Each stage is idempotent and can be run alone.
The number prefix is the execution order.

| Stage | What it does |
| --- | --- |
| `10-check-prereqs` | Read-only readiness report: toolchain, disk, memory. |
| `20-install-llama-cpp` | Installs llama.cpp and builds whisper.cpp when missing. |
| `30-fetch-model-qwen3` | Default heavyweight local model: Qwen3.8-27B + MTP draft. |
| `31-fetch-model-gemma4` | Junior-tier model: gemma-4-E4B-it + projector. Required. |
| `32-fetch-model-muse-glimmer` | Optional deliberator: Muse-Glimmer-30B + DFlash draft. |
| `40-login-anthropic` | Claude Code install and subscription sign-in. |
| `41-login-chatgpt` | Codex CLI install and subscription sign-in. |
| `50-set-default-stack` | Materializes `.env`, verifies the registry models, writes the resident-model cap. |
| `60-verify-boot` | Runs `scripts/first-run-check.sh`; non-zero exit when the machine is not ready. |

Model sources and file names are pinned once, in [scripts/download-models.sh](../download-models.sh).
The PowerShell mirror of that table lives in `_boot_lib.ps1` and must stay in step with it.

## Flags

`boot.sh` accepts `--with-glimmer`, `--skip-models`, `--skip-logins`, and `--force-models`.
`boot.ps1` accepts the same as `-WithGlimmer`, `-SkipModels`, `-SkipLogins`, and `-ForceModels`.

## Windows

Native Windows runs the model stack and the sign-ins: llama.cpp (winget `ggml.llamacpp`), the GGUF downloads, Claude Code, and Codex.
The orchestration runtime itself targets macOS and Linux, so run it inside WSL2 and either point `LOCAL_AGENT_LLAMA_MODELS_DIR` at the native model directory or refetch inside WSL.
`60-verify-boot.ps1` prints the exact steps.

## After boot

```bash
./scripts/start-agent-runtime.sh
uv run pi /start /new-project
```

Or attach your own AI tool over MCP; see [skills/operate-agent-os/SKILL.md](../../skills/operate-agent-os/SKILL.md).
