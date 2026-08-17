# Onboarding: the single lane

One path from a fresh machine to a governed dispatch.
Every step is a script you can read, each is idempotent, and the whole lane is drawn as a DAG in [../diagrams/aidashos-onboarding-dag.png](../diagrams/aidashos-onboarding-dag.png) (editable source: [aidashos-onboarding-dag.excalidraw](../diagrams/aidashos-onboarding-dag.excalidraw)).

Nothing in this lane reports anywhere.
Cloning, booting, and running are invisible to the author; the only counting happens on aidashos.com itself, which counts its own button clicks.

## 1. Clone and base install

```bash
git clone https://github.com/rahul-nath/aidashos.git && cd aidashos && make
```

`make` runs `scripts/bootstrap.sh --install-system`: uv and Python 3.13, Node, `.env` from the example, Docker, Postgres, and the schemas.
It downloads no model weights.

## 2. Boot sequence

Two equivalent ways to run it.

By hand:

```bash
./scripts/boot/boot.sh
```

Or by agent: paste [BOOT_PROMPT.md](BOOT_PROMPT.md) into any AI tool with shell access, opened at the repo root.

Either way the stages are the same, and [scripts/boot/README.md](../../scripts/boot/README.md) documents each one:

1. `10-check-prereqs` reports the toolchain, disk, and memory.
2. `20-install-llama-cpp` installs llama.cpp and builds whisper.cpp.
3. `30-fetch-model-qwen3` fetches the default heavyweight local model, Qwen3.8-27B.
4. `31-fetch-model-gemma4` fetches the junior-tier model the system will not run without.
5. `32-fetch-model-muse-glimmer` is optional (`--with-glimmer`): the Muse-Glimmer-30B deliberator.
6. `40-login-anthropic` and `41-login-chatgpt` sign your existing Claude and ChatGPT subscriptions in through their own CLIs.
   There are no API keys; the flows are interactive and the scripts never see your credentials.
7. `50-set-default-stack` materializes `.env`, checks the model registry against what you downloaded, and caps the llama router at one resident heavyweight model when both are installed.
8. `60-verify-boot` runs `scripts/first-run-check.sh` and fails loudly with the fixing command for anything missing.

The opinionated defaults you inherit are the checked-in configs: `configs/staffing.toml` seats senior on Codex and staff on Claude Code with gemma4 as the local junior, and `configs/model_registry.toml` maps the local roles.
Swapping any seat is a one-line TOML edit.

## 3. Run

```bash
./scripts/start-agent-runtime.sh
```

Postgres, the llama.cpp router, whisper, and the resident pi daemon come up supervised; the script exits non-zero naming anything that failed.

## 4. Drive it

From the terminal:

```bash
uv run pi /start /new-project
uv run pi /approve-most-recent
uv run pi /dispatch
uv run pi /ledger
```

Or from your own AI tool over MCP: Claude Code picks up the repo's `.mcp.json` automatically, and [skills/operate-agent-os/SKILL.md](../../skills/operate-agent-os/SKILL.md) carries the Codex config block plus the operating ritual for any agent.

## Platforms

macOS is the supported platform today, and it is the one this is developed and run on daily.
Linux is expected to work, since every boot stage is POSIX shell and the runtime has no macOS-specific dependency, but it is not exercised on a schedule.

Windows is not supported.
PowerShell twins of every boot stage are written and kept at `potential_directions/windows-boot/`, and they have never been executed or parsed, so nothing here claims they work.
`potential_directions/windows-boot/README.md` says what would have to be true to bring them back.

## When something blocks

`./scripts/first-run-check.sh` is the always-current answer to "what is this machine still missing".
Every blocked line prints the command that fixes it, and every boot stage can be re-run alone.
