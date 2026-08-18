#!/usr/bin/env bash
set -uo pipefail

# Is this machine ready to run a governed task, and if not, what exactly is missing?
#
# `bootstrap.sh --check-only` answers a different question: are the system
# dependencies installed. This one asks whether the system can actually do its
# work, which fails for reasons bootstrap cannot see - a frontier CLI that is
# installed but signed out, a junior model that was never downloaded, a target
# project whose path does not exist yet.
#
# Every failure prints the command that fixes it. A check that says "not ready"
# without saying what to do sends an operator into the source to find out, and
# the answer is never in the place they look first.
#
# Read-only by construction: nothing here starts, stops, installs, or writes.
# One opt-in exception: --probe-frontier-models spends one tiny completion per
# staffed frontier model, because a model id that no longer exists otherwise
# surfaces as a dispatch failure at spawn time, mid-run, on somebody's quota.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROBE_FRONTIER=0
for argument in "$@"; do
  case "$argument" in
    --probe-frontier-models) PROBE_FRONTIER=1 ;;
    *)
      printf 'unknown option: %s\nusage: %s [--probe-frontier-models]\n' "$argument" "$0" >&2
      exit 2
      ;;
  esac
done

READY=0
BLOCKED=0
OPTIONAL=0

ok()      { printf '  \033[32mok\033[0m       %s\n' "$1"; READY=$((READY + 1)); }
blocked() { printf '  \033[31mblocked\033[0m  %s\n' "$1"; printf '           fix: %s\n' "$2"; BLOCKED=$((BLOCKED + 1)); }
partial() { printf '  \033[33mmissing\033[0m  %s\n' "$1"; printf '           fix: %s\n' "$2"; OPTIONAL=$((OPTIONAL + 1)); }
section() { printf '\n%s\n' "$1"; }

section "Toolchain"
if command -v uv >/dev/null 2>&1; then
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
else
  blocked "uv is not installed" "./scripts/bootstrap.sh --install-system"
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  ok "docker is running"
else
  blocked "docker is not running" "start Docker Desktop, then ./scripts/start-docker-compose-infra.sh postgres"
fi

section "Durable ledger"
# The ledger is the recovery and audit authority, so an unreachable one is not a
# degraded mode: every dispatch, approval, and lease needs it.
#
# Reachable and usable are two questions, and this asks both. A checkout now
# refuses when the runtime and the database disagree about the schema, so an
# operator who has just pulled a schema bump would otherwise read "unreachable"
# about a server that is up and answering, and go start a container that is
# already running. The state is read without touching the schema: a readiness
# check that migrated in order to report on migration would be its own worst
# finding.
LEDGER_STATE="$(uv run python -c "
from local_first_agent_os.coordination.store import coordination_schema_state
state = coordination_schema_state()
print(f\"{state['state']} {state['applied_version']} {state['runtime_version']} {state['target']}\")
" 2>/dev/null)" || LEDGER_STATE=""
case "${LEDGER_STATE%% *}" in
  CURRENT|ABSENT)
    ok "coordination Postgres is reachable"
    ;;
  MIGRATION_REQUIRED)
    read -r _ applied runtime target <<<"$LEDGER_STATE"
    blocked "coordination database needs migration: $target is at schema $applied, this runtime is at $runtime" \
      "agent-ledger migrate_coordination_schema"
    ;;
  NEWER_THAN_RUNTIME)
    read -r _ applied runtime target <<<"$LEDGER_STATE"
    blocked "coordination database is newer than this checkout: $target is at schema $applied, this runtime is at $runtime" \
      "git pull (this checkout is behind the ledger; migrating is not the fix)"
    ;;
  *)
    blocked "coordination Postgres is unreachable" "./scripts/start-docker-compose-infra.sh postgres"
    ;;
esac

section "Local junior model (required)"
# The one dependency the system will not run without. The junior tier decides
# things *about* the frontier agents - permission-envelope scans, stall
# adjudication, review-progress classification - so a machine without it is an
# agent OS that cannot think without somebody else's network.
LLAMA_URL="${LOCAL_AGENT_LLAMA_BASE_URL:-http://127.0.0.1:8080}"
if curl -fsS --max-time 3 "$LLAMA_URL/health" >/dev/null 2>&1 \
  || curl -fsS --max-time 3 "$LLAMA_URL/v1/models" >/dev/null 2>&1; then
  ok "llama.cpp router is serving at $LLAMA_URL"
else
  MODELS_DIR="${LOCAL_AGENT_LLAMA_MODELS_DIR:-$HOME/models}"
  if compgen -G "$MODELS_DIR/*.gguf" >/dev/null 2>&1; then
    blocked "llama.cpp is not serving, but weights are present in $MODELS_DIR" \
      "./scripts/start-agent-runtime.sh"
  else
    blocked "no local model weights found in $MODELS_DIR" \
      "./scripts/download-models.sh --list, then ./scripts/download-models.sh gemma4"
  fi
fi

section "Frontier subscriptions (required for the coding pipeline)"
# Optional for local queries and junior-only work; required for senior
# implementation and staff review. Signed out is the interesting failure,
# because an installed-but-signed-out CLI looks present to every other check.
if command -v claude >/dev/null 2>&1; then
  ok "claude CLI $(claude --version 2>/dev/null | head -1)"
  printf '           check login: claude doctor\n'
else
  partial "claude CLI is not installed (senior tier unavailable)" \
    "./scripts/install-frontier-clis.sh --install, then run: claude"
fi

if command -v codex >/dev/null 2>&1; then
  if codex login status >/dev/null 2>&1; then
    ok "codex CLI is signed in"
  else
    partial "codex CLI is installed but signed out (staff review unavailable)" "codex login"
  fi
else
  partial "codex CLI is not installed (staff review unavailable)" \
    "./scripts/install-frontier-clis.sh --install, then: codex login"
fi

section "Frontier models (as staffed)"
# These rows are read the way the dispatcher reads them, so they are what a
# dispatch would actually spawn. Model ids are passed to the CLIs verbatim and
# nothing validates them at load; --probe-frontier-models makes each staffed id
# answer a one-line completion now, instead of failing a dispatch later.
PROBE_FRONTIER="$PROBE_FRONTIER" uv run python - <<'PY'
import os
import subprocess

from local_first_agent_os.settings import get_settings
from local_first_agent_os.staffing import FrontierHarness, classify_harness, load_bench

OK = "  \033[32mok\033[0m      "
MISSING = "  \033[33mmissing\033[0m "
probe = os.environ.get("PROBE_FRONTIER") == "1"
bench = load_bench(get_settings().config_dir / "staffing.toml")


def nonce_command(kind: FrontierHarness, model: str | None) -> list[str]:
    if kind is FrontierHarness.CLAUDE:
        command = ["claude", "--print"]
    else:
        command = ["codex", "exec", "--skip-git-repo-check"]
    if model:
        command += ["--model", model]
    command.append("Reply with exactly: ok")
    return command


for tier, slot in sorted(bench.items(), key=lambda item: item[0].value):
    kind = classify_harness(slot.harness)
    if not isinstance(kind, FrontierHarness):
        continue
    label = f"{tier.value}: {slot.harness.value} --model {slot.model or '(CLI default)'}"
    if not probe:
        print(f"           {label}  (unproved; --probe-frontier-models proves the id)")
        continue
    try:
        completed = subprocess.run(
            nonce_command(kind, slot.model),
            capture_output=True,
            text=True,
            timeout=180,
        )
    except FileNotFoundError:
        print(f"{MISSING} {label}: the {slot.harness.value} CLI is not installed")
        print("           fix: ./scripts/install-frontier-clis.sh --install")
        continue
    except subprocess.TimeoutExpired:
        print(f"{MISSING} {label}: no answer within 180s")
        continue
    if completed.returncode == 0:
        print(f"{OK} {label} answered a nonce completion")
    else:
        lines = (completed.stderr.strip() or completed.stdout.strip()).splitlines()
        detail = lines[-1] if lines else f"exit {completed.returncode}"
        print(f"{MISSING} {label}: {detail}")
        spelling = (
            "claude --model <id> --print ok"
            if kind is FrontierHarness.CLAUDE
            else "codex exec --skip-git-repo-check --model <id> ok"
        )
        print(f"           fix: prove a candidate id first: {spelling}")
PY

section "Target projects"
# A target project is a workspace this control plane is allowed to touch, not a
# dependency of the OS. The registry is operator state: a fresh clone ships
# examples that describe no real machine, and a dispatch aimed at one fails
# closed until its path exists. New projects do not need an entry written by
# hand - `/start /new-project` scaffolds the repository and registers it.
uv run python - <<'PY'
import sys
from pathlib import Path

try:
    from local_first_agent_os.project_center import load_project_center
    from local_first_agent_os.settings import get_settings

    settings = get_settings()
    center = load_project_center(settings)
except Exception as error:  # noqa: BLE001 - this is a report, not a control path
    print(f"  \033[31mblocked\033[0m  linked project registry: {type(error).__name__}: {error}")
    print("           fix: edit configs/linked_projects.toml so [center] names ids it defines")
    sys.exit(0)

missing = [p for p in center.projects if not p.expanded_path.exists()]
present = [p for p in center.projects if p.expanded_path.exists()]
for project in present:
    print(f"  \033[32mok\033[0m       {project.id} -> {project.expanded_path}")
for project in missing:
    print(f"  \033[33mmissing\033[0m  {project.id} -> {project.expanded_path} does not exist")
if missing:
    print("           fix: point it at a repository you have, or let the intake create it:")
    print(f"           pi /start /new-project --target-project-id {missing[0].id}")
    print("           (a dispatch at a missing path fails closed; nothing else is affected)")
PY

section "Resident loops"
# The two processes that make queued work move. Supervised by launchd, so the
# expected steady state is owned-by-someone rather than started-by-you.
if uv run python scripts/resident_loop_owners.py 2>/dev/null | grep -q .; then
  uv run python scripts/resident_loop_owners.py 2>/dev/null | while IFS=$'\t' read -r loop _pid description; do
    printf '  \033[32mok\033[0m       %s: %s\n' "$loop" "$description"
  done
else
  partial "no resident loop owns the drainer or the dispatcher" \
    "./scripts/launchd/install.sh (supervised), or ./scripts/start-agent-runtime.sh (this shell)"
fi

section "Summary"
printf '  %d ready, %d blocked, %d optional gaps\n' "$READY" "$BLOCKED" "$OPTIONAL"
if [ "$BLOCKED" -gt 0 ]; then
  printf '\n  Blocked items stop a governed run. Fix those first.\n'
  exit 1
fi
printf '\n  Ready. Next: pi /start /new-project --target-project-id <your-project>\n'
