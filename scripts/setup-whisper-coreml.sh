#!/usr/bin/env bash
set -euo pipefail

# Install the prebuilt Core ML encoder sidecar for the registry's
# large-v3-turbo ASR model, then rebuild whisper.cpp with Core ML, Core ML
# fallback, and Metal enabled. Core ML handles the Whisper encoder; Metal/CPU
# remain available for the rest of whisper.cpp.

WHISPER_CPP_DIR="${WHISPER_CPP_DIR:-${LOCAL_AGENT_WHISPER_CPP_DIR:-${LOCAL_AGENT_PROJECTS_ROOT:-$HOME/ai_projects}/whisper.cpp}}"
MODEL_NAME="${LOCAL_AGENT_WHISPER_MODEL_NAME:-large-v3-turbo}"
MODELS_DIR="${LOCAL_AGENT_WHISPER_MODELS_DIR:-$WHISPER_CPP_DIR/models}"
BUILD_DIR="${WHISPER_BUILD_DIR:-$WHISPER_CPP_DIR/build}"
MODEL_PATH="${LOCAL_AGENT_WHISPER_MODEL_PATH:-$MODELS_DIR/ggml-$MODEL_NAME.bin}"
COREML_DIR="${LOCAL_AGENT_WHISPER_COREML_PATH:-$MODELS_DIR/ggml-$MODEL_NAME-encoder.mlmodelc}"
COREML_ZIP="${COREML_DIR}.zip"
COREML_URL="${LOCAL_AGENT_WHISPER_COREML_URL:-https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$MODEL_NAME-encoder.mlmodelc.zip}"
JOBS="${JOBS:-$(sysctl -n hw.ncpu 2>/dev/null || echo 4)}"

for tool in curl unzip cmake; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Missing required tool: $tool" >&2
    exit 127
  fi
done

if [ ! -d "$WHISPER_CPP_DIR" ]; then
  echo "whisper.cpp directory does not exist: $WHISPER_CPP_DIR" >&2
  exit 2
fi

mkdir -p "$MODELS_DIR"

if [ ! -f "$MODEL_PATH" ]; then
  echo "Whisper GGML model is missing: $MODEL_PATH" >&2
  echo "Install the GGML model first, or set LOCAL_AGENT_WHISPER_MODEL_PATH." >&2
  exit 2
fi

if [ ! -d "$COREML_DIR" ]; then
  echo "Downloading Core ML encoder sidecar:"
  echo "  $COREML_URL"
  curl --connect-timeout 30 --max-time 3600 -L --fail --output "$COREML_ZIP" "$COREML_URL"
  echo "Extracting $COREML_ZIP into $MODELS_DIR"
  unzip -q -o "$COREML_ZIP" -d "$MODELS_DIR"
else
  echo "Core ML encoder sidecar already exists: $COREML_DIR"
fi

if [ ! -d "$COREML_DIR" ]; then
  echo "Expected Core ML sidecar directory was not created: $COREML_DIR" >&2
  exit 2
fi

echo "Building whisper.cpp with Core ML fallback and Metal enabled:"
echo "  build dir: $BUILD_DIR"
cmake -S "$WHISPER_CPP_DIR" -B "$BUILD_DIR" \
  -DWHISPER_COREML=1 \
  -DWHISPER_COREML_ALLOW_FALLBACK=1 \
  -DGGML_METAL=ON
cmake --build "$BUILD_DIR" -j "$JOBS" --config Release

echo "Verifying CMake flags"
grep -Eq '^WHISPER_COREML:BOOL=(ON|1|TRUE)$' "$BUILD_DIR/CMakeCache.txt"
grep -Eq '^WHISPER_COREML_ALLOW_FALLBACK:BOOL=(ON|1|TRUE)$' "$BUILD_DIR/CMakeCache.txt"
grep -Eq '^GGML_METAL:BOOL=(ON|1|TRUE)$' "$BUILD_DIR/CMakeCache.txt"

echo "whisper.cpp Core ML setup complete."
echo "  model:   $MODEL_PATH"
echo "  sidecar: $COREML_DIR"
echo "  server:  $BUILD_DIR/bin/whisper-server"
