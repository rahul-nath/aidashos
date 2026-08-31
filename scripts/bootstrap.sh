#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=toolchain-pins.env
. "$ROOT/scripts/toolchain-pins.env"
INSTALL_SYSTEM=false
WITH_MODEL_RUNTIMES=false
WITH_FRONTIER_CLIS=false
CHECK_ONLY=false

usage() {
  cat <<'EOF'
Usage: ./scripts/bootstrap.sh [options]

  --install-system         Install missing uv/Docker prerequisites.
  --with-model-runtimes    Install llama.cpp and build whisper.cpp (no models).
  --with-frontier-clis     Install Codex CLI and Claude Code (login stays interactive).
  --check-only             Report readiness without changing the machine.

Large model downloads are never automatic. Use ./scripts/download-models.sh --list.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-system) INSTALL_SYSTEM=true ;;
    --with-model-runtimes) WITH_MODEL_RUNTIMES=true ;;
    --with-frontier-clis) WITH_FRONTIER_CLIS=true ;;
    --check-only) CHECK_ONLY=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

os="$(uname -s)"

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    [ "$(uv --version | awk '{print $2}')" = "$UV_VERSION" ] && return
    echo "uv $(uv --version | awk '{print $2}') is installed; this checkout requires $UV_VERSION." >&2
    exit 1
  fi
  if [ "$INSTALL_SYSTEM" != true ]; then
    echo "uv is missing. Re-run with --install-system." >&2
    exit 1
  fi
  local architecture archive checksum platform temporary
  architecture="$(uname -m)"
  case "$os:$architecture" in
    Darwin:arm64) platform="aarch64-apple-darwin"; checksum="$UV_SHA256_DARWIN_ARM64" ;;
    Darwin:x86_64) platform="x86_64-apple-darwin"; checksum="$UV_SHA256_DARWIN_X86_64" ;;
    Linux:aarch64|Linux:arm64) platform="aarch64-unknown-linux-gnu"; checksum="$UV_SHA256_LINUX_ARM64" ;;
    Linux:x86_64) platform="x86_64-unknown-linux-gnu"; checksum="$UV_SHA256_LINUX_X86_64" ;;
    *) echo "No pinned uv artifact for $os $architecture." >&2; exit 1 ;;
  esac
  archive="uv-${platform}.tar.gz"
  temporary="$(mktemp -d)"
  trap 'rm -rf "$temporary"' EXIT
  curl --proto '=https' --tlsv1.2 --connect-timeout 15 --max-time 120 -LsSf \
    "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${archive}" \
    -o "$temporary/$archive"
  printf '%s  %s\n' "$checksum" "$temporary/$archive" | shasum -a 256 -c -
  tar -xzf "$temporary/$archive" -C "$temporary"
  mkdir -p "$HOME/.local/bin"
  install -m 0755 "$temporary/uv-${platform}/uv" "$HOME/.local/bin/uv"
  install -m 0755 "$temporary/uv-${platform}/uvx" "$HOME/.local/bin/uvx"
  rm -rf "$temporary"
  trap - EXIT
  export PATH="$HOME/.local/bin:$PATH"
}

install_docker() {
  if command -v docker >/dev/null 2>&1; then return; fi
  if [ "$INSTALL_SYSTEM" != true ]; then
    echo "Docker is missing. Re-run with --install-system." >&2
    exit 1
  fi
  case "$os" in
    Darwin)
      command -v brew >/dev/null 2>&1 || {
        echo "Homebrew is required to install Docker Desktop automatically." >&2
        exit 1
      }
      brew install --cask docker
      open -a Docker
      ;;
    Linux)
      sudo apt-get update
      sudo apt-get install -y docker.io docker-compose-plugin
      sudo usermod -aG docker "$USER"
      echo "Docker group membership changed; log out/in if docker remains unavailable." >&2
      ;;
    *) echo "Unsupported platform: $os" >&2; exit 1 ;;
  esac
}

install_node() {
  local wanted
  wanted="$NODE_VERSION"
  [ "$(cat "$ROOT/.node-version")" = "$wanted" ] || {
    echo ".node-version and toolchain-pins.env disagree" >&2
    exit 1
  }
  if command -v node >/dev/null 2>&1 && [ "$(node --version)" = "v$wanted" ]; then
    return
  fi
  if [ "$INSTALL_SYSTEM" != true ]; then
    echo "Node $wanted is missing. Re-run with --install-system." >&2
    exit 1
  fi
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  if [ ! -s "$NVM_DIR/nvm.sh" ]; then
    git clone --branch "$NVM_VERSION" --depth 1 https://github.com/nvm-sh/nvm.git "$NVM_DIR"
  elif [ "$(git -C "$NVM_DIR" describe --tags --exact-match 2>/dev/null || true)" != "$NVM_VERSION" ]; then
    echo "$NVM_DIR is not at pinned NVM tag $NVM_VERSION; reconcile it explicitly." >&2
    exit 1
  fi
  # shellcheck source=/dev/null
  source "$NVM_DIR/nvm.sh"
  nvm install "$wanted"
  nvm use "$wanted"
}

check_command() {
  local name="$1"
  if command -v "$name" >/dev/null 2>&1; then
    printf 'ok      %s: %s\n' "$name" "$(command -v "$name")"
  else
    printf 'missing %s\n' "$name"
  fi
}

if [ "$CHECK_ONLY" = true ]; then
  check_command git
  check_command uv
  check_command docker
  check_command node
  check_command llama-server
  check_command codex
  check_command claude
  [ -f "$ROOT/.env" ] && echo "ok      .env" || echo "missing .env"
  [ -x "${LOCAL_AGENT_WHISPER_BIN_PATH:-$HOME/ai_projects/whisper.cpp/build/bin/whisper-server}" ] \
    && echo "ok      whisper-server" || echo "missing whisper-server"
  exit 0
fi

install_uv
uv python install 3.13
uv sync
install_node

if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "Created .env from portable defaults. Review optional API and observability keys."
fi
mkdir -p "$HOME/.local-agent/artifacts" "$HOME/.local-agent/spool" "$HOME/models"

install_docker
if [ "$os" = "Darwin" ] && ! docker info >/dev/null 2>&1; then
  open -a Docker
  echo "Waiting for Docker Desktop..."
  for _ in {1..120}; do
    docker info >/dev/null 2>&1 && break
    sleep 1
  done
fi
docker info >/dev/null 2>&1 || {
  echo "Docker is installed but its daemon is not ready." >&2
  exit 1
}
"$ROOT/scripts/start-docker-compose-infra.sh" postgres
uv run local-agent init-db

if [ "$WITH_MODEL_RUNTIMES" = true ]; then
  "$ROOT/scripts/install-model-runtimes.sh"
fi
if [ "$WITH_FRONTIER_CLIS" = true ]; then
  "$ROOT/scripts/install-frontier-clis.sh" --install
fi

echo
echo "Bootstrap complete: Python, uv environment, Docker Postgres, and schemas are ready."
echo "Models were not downloaded. Inspect exact sources with:"
echo "  ./scripts/download-models.sh --list"
echo "After installing the required models, start the runtime with:"
echo "  ./scripts/start-agent-runtime.sh"
