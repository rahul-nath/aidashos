# The boot prompt

Paste the block below into any local AI agent (Claude Code, Codex, or another tool with shell access) opened at the repo root.
It drives the same stages as `./scripts/boot/boot.sh`, one at a time, and leaves the sign-ins and the big-download confirmations to you.

The canonical copy of this text is [prompts.json](prompts.json), which the aidashos.com landing page renders with copy buttons.
`tests/test_onboarding_prompts.py` pins this file to it, so edit the JSON first.

```text
You are working inside a fresh clone of aidashos, a local-first agent OS. Read scripts/boot/README.md and docs/onboarding/ONBOARDING.md before acting.

Then complete the boot sequence:

1. Run ./scripts/bootstrap.sh --check-only and report what is missing.
2. If uv or the Python environment is not ready, run `make` and wait for it to finish.
3. Run the boot stages one at a time from scripts/boot/, in numeric order (10-check-prereqs.sh, 20-install-llama-cpp.sh, 30-fetch-model-qwen3.sh, 31-fetch-model-gemma4.sh, 40-login-anthropic.sh, 41-login-chatgpt.sh, 50-set-default-stack.sh, 60-verify-boot.sh), rather than boot.sh, so each failure is visible and fixable. On Windows use the .ps1 twins and follow the WSL2 note in scripts/boot/README.md.
4. Stages 30 and 31 download model weights and are the only slow, large steps. Before each, run `./scripts/download-models.sh --list` to show me exactly which repository and file it will fetch, and wait for my confirmation.
5. The two sign-ins are interactive browser flows. Hand control to me for them and never enter credentials yourself.
6. If a stage fails, read its output: every blocked line prints the command that fixes it. Fix, then re-run the stage; all stages are idempotent.
7. Finish by running ./scripts/boot/60-verify-boot.sh and show me its full output.
```

Two follow-up prompts continue the lane after boot: `first-run` starts the runtime and compiles the example plan, and `attach-tool` connects your own AI tool over MCP.
Both live in [prompts.json](prompts.json) and on the landing page.
