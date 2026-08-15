#!/usr/bin/env bash
set -euo pipefail

MODELS_DIR="${LOCAL_AGENT_LLAMA_MODELS_DIR:-$HOME/models}"
WHISPER_DIR="${LOCAL_AGENT_WHISPER_CPP_DIR:-${LOCAL_AGENT_PROJECTS_ROOT:-$HOME/ai_projects}/whisper.cpp}"

list_models() {
  cat <<'EOF'
gemma4          unsloth/gemma-4-E4B-it-GGUF
                gemma-4-E4B-it-Q4_K_M.gguf + mmproj-F16.gguf
qwen38          unsloth/Qwen3.8-27B-GGUF + ggml-org/Qwen3.8-27B-GGUF
                Qwen3.8-27B-UD-Q5_K_XL.gguf + mtp-Qwen3.8-27B-Q4_0.gguf
glimmer         meta-models/Muse-Glimmer-30B-GGUF
                muse-glimmer-30B-kquant-dynamic.gguf + dflash-kquant.gguf
surya           datalab-to/surya-ocr-2-gguf
                surya-2.gguf + surya-2-mmproj.gguf
chandra         mradermacher/chandra-ocr-2-GGUF
                chandra-ocr-2.Q8_0.gguf + chandra-ocr-2.mmproj-f16.gguf
embedder        Qwen/Qwen3-Embedding-8B-GGUF
                Qwen3-Embedding-8B-Q4_K_M.gguf
medgemma        unsloth/medgemma-1.5-4b-it-GGUF
                medgemma-1.5-4b-it-Q4_K_M.gguf + mmproj-BF16.gguf
whisper         ggerganov/whisper.cpp
                ggml-base.en.bin + ggml-large-v3-turbo.bin

Usage: ./scripts/download-models.sh <name|all>
Downloads are explicit and may require accepting a model repository's terms.
EOF
}

hf_download() {
  local repo="$1" file="$2" dir="$3" local_name="$4"
  mkdir -p "$dir"
  uvx --from huggingface-hub hf download "$repo" "$file" --local-dir "$dir"
  if [ "$file" != "$local_name" ]; then
    ln -sfn "$file" "$dir/$local_name"
  fi
}

download_one() {
  case "$1" in
    gemma4)
      hf_download unsloth/gemma-4-E4B-it-GGUF gemma-4-E4B-it-Q4_K_M.gguf "$MODELS_DIR/gemma4" model.gguf
      hf_download unsloth/gemma-4-E4B-it-GGUF mmproj-F16.gguf "$MODELS_DIR/gemma4" mmproj.gguf
      ;;
    qwen38)
      # Two files from two repositories, which is the part worth reading before
      # editing. Unsloth's UD-Q5_K_XL is the target: 20.2GB against ggml-org's
      # 19.0GB Q4_K_M for the same weights, which on a 36GB machine is a
      # 1.2GB premium this tier can afford and the quant it replaced could not
      # justify refusing. Unsloth publishes no MTP drafter, so the draft still
      # comes from ggml-org, and the registry's `draft_gguf_path` points
      # llama.cpp at it.
      #
      # Mixing vendors across a speculative pair is an inference, not a
      # guarantee: both derive from Qwen/Qwen3.8-27B, and the target verifies
      # every proposal, so a mismatch would show up as collapsed acceptance
      # rather than as wrong output. Measured on 2026-08-15 rather than assumed:
      # 0.82 and 0.71 acceptance across two turns, mean draft length ~2.5,
      # against the 0.72-0.76 the qwen3.6 pair used to return.
      hf_download unsloth/Qwen3.8-27B-GGUF Qwen3.8-27B-UD-Q5_K_XL.gguf "$MODELS_DIR/qwen3.8-27b-mtp" model.gguf
      hf_download ggml-org/Qwen3.8-27B-GGUF mtp-Qwen3.8-27B-Q4_0.gguf "$MODELS_DIR/qwen3.8-27b-mtp" draft.gguf
      ;;
    glimmer)
      # The deliberator seat in configs/model_registry.toml. The draft file is a
      # DFlash speculative drafter, not MTP, so the registry declares
      # `type = "draft-dflash"` for it. Roughly 20GB resident: on a 36GB machine
      # this and qwen38 must not be loaded at once, which is what the
      # LOCAL_AGENT_LLAMA_MODELS_MAX=1 guard in scripts/boot/50-set-default-stack.sh
      # exists for.
      hf_download meta-models/Muse-Glimmer-30B-GGUF muse-glimmer-30B-kquant-dynamic.gguf "$MODELS_DIR/glimmer" model.gguf
      hf_download meta-models/Muse-Glimmer-30B-GGUF dflash-kquant.gguf "$MODELS_DIR/glimmer" draft.gguf
      ;;
    surya)
      hf_download datalab-to/surya-ocr-2-gguf surya-2.gguf "$MODELS_DIR/surya-ocr-2" model.gguf
      hf_download datalab-to/surya-ocr-2-gguf surya-2-mmproj.gguf "$MODELS_DIR/surya-ocr-2" mmproj.gguf
      ;;
    chandra)
      hf_download mradermacher/chandra-ocr-2-GGUF chandra-ocr-2.Q8_0.gguf "$MODELS_DIR/chandra-ocr-2-q8" model.gguf
      hf_download mradermacher/chandra-ocr-2-GGUF chandra-ocr-2.mmproj-f16.gguf "$MODELS_DIR/chandra-ocr-2-q8" mmproj.gguf
      ;;
    embedder)
      hf_download Qwen/Qwen3-Embedding-8B-GGUF Qwen3-Embedding-8B-Q4_K_M.gguf "$MODELS_DIR/qwen-embed-8b" model.gguf
      ;;
    medgemma)
      hf_download unsloth/medgemma-1.5-4b-it-GGUF medgemma-1.5-4b-it-Q4_K_M.gguf "$MODELS_DIR/medgemma-4b" model.gguf
      hf_download unsloth/medgemma-1.5-4b-it-GGUF mmproj-BF16.gguf "$MODELS_DIR/medgemma-4b" mmproj.gguf
      ;;
    whisper)
      hf_download ggerganov/whisper.cpp ggml-base.en.bin "$WHISPER_DIR/models" ggml-base.en.bin
      hf_download ggerganov/whisper.cpp ggml-large-v3-turbo.bin "$WHISPER_DIR/models" ggml-large-v3-turbo.bin
      ;;
    *) echo "Unknown model: $1" >&2; list_models >&2; exit 2 ;;
  esac
}

case "${1:---list}" in
  --list|-l) list_models ;;
  all)
    for model in gemma4 qwen38 glimmer surya chandra embedder medgemma whisper; do download_one "$model"; done
    ;;
  *) download_one "$1" ;;
esac
