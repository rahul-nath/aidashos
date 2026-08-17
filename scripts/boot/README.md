# The boot sequence

Everything after `make`, in dependency order.
`make` leaves a working uv environment, Docker Postgres, and schemas.
The boot sequence takes the machine the rest of the way: local model runtime, model weights, frontier subscriptions, stack config, and a final verification.

The full picture is drawn in [docs/diagrams/aidashos-onboarding-dag.png](../../docs/diagrams/aidashos-onboarding-dag.png).

## One command

```bash
./scripts/boot/boot.sh
```

macOS is the supported platform; Linux is expected to work and is not exercised on a schedule.
Windows is not supported, and `potential_directions/windows-boot/README.md` says why.

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

## Flags

`boot.sh` accepts `--with-glimmer`, `--skip-models`, `--skip-logins`, and `--force-models`.

## After boot

```bash
./scripts/start-agent-runtime.sh
uv run pi /start /new-project
```

Or attach your own AI tool over MCP; see [skills/operate-agent-os/SKILL.md](../../skills/operate-agent-os/SKILL.md).
