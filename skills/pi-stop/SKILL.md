# Pi Stop

Use this skill when a terminal user wants Pi to unload local model roles. `/stop` is the durable counterpart of `/start`.

Canonical directives:

```bash
pi /stop
pi /stop /ocr
pi /stop /asr
pi /stop /med
pi /stop /gemma4
pi /stop /fallback
pi /stop /compactor
```

Execution semantics:

1. Bare `/stop` unloads llama.cpp roles through the router and stops the separate whisper.cpp ASR service.
2. `/stop /<alias>` resolves to a single role through `configs/directives.toml` aliases or `~/models/<alias>` lookup.
3. The directive runs as a durable `model_directive` workflow and records a `directive_result.v1` artifact with the unload outcome.
4. `/stop` is invoked automatically by the terminal-session hook when the last non-VS Code shell exits, so eligible terminals own the model lifecycle.
5. The compaction workflow unloads the `compactor` role through its scoped model session because that role has `warm_ttl_seconds=0`.
6. `/stop /asr` boots out the local launchd whisper job before terminating the server so `KeepAlive` cannot respawn it. A later `/start /asr` bootstraps the job again.

Aliases and chainable forms:

```bash
pi /stop /fallback
pi /start /ocr /stop /med
```

Design note: `/stop` does not require sudo because unloading does not consume new resources. It is safe to chain `/stop` with other directives without re-prompting for sudo.
