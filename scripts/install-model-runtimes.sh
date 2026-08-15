#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
      git clone --depth 1 https://github.com/ggerganov/llama.cpp.git "$LLAMA_DIR"
    fi
    cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -DGGML_NATIVE=ON -DLLAMA_CURL=ON
    cmake --build "$LLAMA_DIR/build" --config Release -j
    cmake --install "$LLAMA_DIR/build" --prefix "$HOME/.local"
    ;;
  *) echo "Unsupported platform: $os" >&2; exit 1 ;;
esac

if [ ! -d "$WHISPER_DIR/.git" ]; then
  mkdir -p "$(dirname "$WHISPER_DIR")"
  git clone --depth 1 https://github.com/ggerganov/whisper.cpp.git "$WHISPER_DIR"
else
  git -C "$WHISPER_DIR" pull --ff-only
fi

whisper_flags=(-DGGML_NATIVE=ON)
if [ "$os" = "Darwin" ]; then
  whisper_flags+=(-DWHISPER_COREML=1 -DWHISPER_COREML_ALLOW_FALLBACK=1 -DGGML_METAL=ON)
fi
cmake -S "$WHISPER_DIR" -B "$WHISPER_DIR/build" "${whisper_flags[@]}"
cmake --build "$WHISPER_DIR/build" --config Release -j

echo "llama.cpp and whisper.cpp runtimes are ready. No model weights were downloaded."
echo "Next: $ROOT/scripts/download-models.sh --list"
