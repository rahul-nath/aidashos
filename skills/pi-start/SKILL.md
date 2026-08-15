# Pi Start

Use this skill when a terminal user wants Pi to load (or warm) a local model role. `/start` is the durable model-lifecycle directive.

Canonical directives:

```bash
pi /start
pi /start /chandra
pi /start /asr
pi /start /ocr
pi /start /med
pi /start /gemma4
pi /start /fallback
pi /start /compactor
pi /start /qwen3.8
pi /start what owns workflow truth?
```

Execution semantics:

1. Bare `/start` resolves to the configured `default_model_role` (default `general`, alias for gemma4).
2. A trailing slash directive (for example `/chandra` or `/ocr` for the OCR role, `/asr` or `/audio` for the ASR role, `/med` for the medical role, `/gemma4` for the general role, `/fallback` or `/qwen3.8` for the general fallback role, `/compactor` for the context compactor) resolves through `configs/directives.toml` aliases or via lookup of a directory under `~/models`.
3. The directive runs as a durable `model_directive` workflow that records a `directive_result.v1` artifact for every load attempt.
4. Plain text after the slash directive (or after `/start`) is appended as a general-model query and runs after the model has been loaded.
5. Loading is sudo-gated. The first `/start` per shell calls `sudo -v` so model loads are tied to an interactive user.
6. If the default model fails to load, Pi falls back through the configured `general_fallback` role plus the pgvector store and reports a degraded status; it still issues a manual-unload warning when memory is tight.
7. ASR loads through `AudioTranscriber` and the separate whisper.cpp server, not the llama.cpp router. If `/stop /asr` previously shut the service down, `/start /asr` starts its launchd job or tracked fallback process first.

Aliases and chainable forms:

```bash
pi /start /ocr /start /asr /start what owns workflow truth?
pi /start /ocr /absolute/path/to/image.png summarize this
pi /start /store /absolute/path/to/directory
```

Permission gates:

- `pi.permission_gate.model_load.v1` — interactive sudo unless `LOCAL_AGENT_SKIP_SUDO_FOR_MODEL_LOAD=true`.
- `pi.permission_gate.filesystem_store.v1` — `/start /store` requires an explicit local path and fails hard when nothing supported is found.

Design note: consecutive `/start` directives in the same call are dispatched in parallel via a thread pool so multi-role warm-ups (`/start /ocr /start /asr /start`) finish in one durable boundary.
