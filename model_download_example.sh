#!/usr/bin/env bash
set -euo pipefail

MODEL_SHORTNAME="gemma4"
HF_REPO="unsloth/gemma-4-E4B-it-GGUF"

MODEL_FILE="gemma-4-E4B-it-Q4_K_M.gguf"
MMPROJ_FILE="mmproj-F16.gguf"

TARGET_DIR="$HOME/models/$MODEL_SHORTNAME"

MODEL_URL="https://huggingface.co/${HF_REPO}/resolve/main/${MODEL_FILE}"
MMPROJ_URL="https://huggingface.co/${HF_REPO}/resolve/main/${MMPROJ_FILE}"

mkdir -p "$TARGET_DIR"

echo "Downloading model to $TARGET_DIR/model.gguf"
curl -fL --retry 3 --continue-at - \
  -o "$TARGET_DIR/model.gguf" \
  "$MODEL_URL"

echo "Downloading projector to $TARGET_DIR/mmproj.gguf"
curl -fL --retry 3 --continue-at - \
  -o "$TARGET_DIR/mmproj.gguf" \
  "$MMPROJ_URL"

cat > "$TARGET_DIR/README.md" <<EOF
# ${MODEL_SHORTNAME}
Source: ${HF_REPO}
File: ${MODEL_FILE}
Projector: ${MMPROJ_FILE}
Local: ${TARGET_DIR}/model.gguf + ${TARGET_DIR}/mmproj.gguf
Quant: ex: Q4_K_M model, F16 projector
Role: ex: context window compactor
EOF

echo "Done."
echo "Files:"
echo "  $TARGET_DIR/model.gguf"
echo "  $TARGET_DIR/mmproj.gguf"
echo "  $TARGET_DIR/README.md"