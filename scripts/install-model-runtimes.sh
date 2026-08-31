#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=toolchain-pins.env
. "$ROOT/scripts/toolchain-pins.env"
PROJECTS_ROOT="${LOCAL_AGENT_PROJECTS_ROOT:-$HOME/ai_projects}"
WHISPER_DIR="${LOCAL_AGENT_WHISPER_CPP_DIR:-$PROJECTS_ROOT/whisper.cpp}"
LLAMA_DIR="${LOCAL_AGENT_LLAMA_CPP_DIR:-$PROJECTS_ROOT/llama.cpp}"
os="$(uname -s)"

case "$os" in
  Darwin)
    command -v brew >/dev/null 2>&1 || {
      echo "Homebrew is required on macOS." >&2
      exit 1
    }
    brew install cmake llama.cpp
    ;;
  Linux)
    sudo apt-get update
    sudo apt-get install -y build-essential cmake git curl
    if [ ! -d "$LLAMA_DIR/.git" ]; then
      git clone --branch "$LLAMA_CPP_REF" --depth 1 \
        https://github.com/ggerganov/llama.cpp.git "$LLAMA_DIR"
    elif [ "$(git -C "$LLAMA_DIR" describe --tags --exact-match 2>/dev/null || true)" != "$LLAMA_CPP_REF" ]; then
      echo "$LLAMA_DIR is not at pinned tag $LLAMA_CPP_REF; reconcile it explicitly." >&2
      exit 1
    fi
    cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -DGGML_NATIVE=ON -DLLAMA_CURL=ON
    cmake --build "$LLAMA_DIR/build" --config Release -j
    cmake --install "$LLAMA_DIR/build" --prefix "$HOME/.local"
    ;;
  *) echo "Unsupported platform: $os" >&2; exit 1 ;;
esac

if [ ! -d "$WHISPER_DIR/.git" ]; then
  mkdir -p "$(dirname "$WHISPER_DIR")"
  git clone --branch "$WHISPER_CPP_REF" --depth 1 \
    https://github.com/ggerganov/whisper.cpp.git "$WHISPER_DIR"
elif [ "$(git -C "$WHISPER_DIR" describe --tags --exact-match 2>/dev/null || true)" != "$WHISPER_CPP_REF" ]; then
  echo "$WHISPER_DIR is not at pinned tag $WHISPER_CPP_REF; reconcile it explicitly." >&2
  exit 1
fi

whisper_flags=(-DGGML_NATIVE=ON)
if [ "$os" = "Darwin" ]; then
  whisper_flags+=(-DWHISPER_COREML=1 -DWHISPER_COREML_ALLOW_FALLBACK=1 -DGGML_METAL=ON)
fi
cmake -S "$WHISPER_DIR" -B "$WHISPER_DIR/build" "${whisper_flags[@]}"
cmake --build "$WHISPER_DIR/build" --config Release -j

echo "llama.cpp and whisper.cpp runtimes are ready. No model weights were downloaded."
echo "Next: $ROOT/scripts/download-models.sh --list"
