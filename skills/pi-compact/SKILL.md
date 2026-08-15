# Pi Compact

Use this skill when terminal context is at or above the configured compaction threshold (default 90% of the configured max window). `/compact` is the durable context-compaction directive.

Canonical directives:

```bash
pi /compact
pi --context-file /tmp/long_context.txt "what owns workflow truth?"
```

Execution semantics:

1. Pi automatically runs the durable `context_compaction` workflow when a pending prompt plus context crosses the configured `compaction_threshold_ratio` (`configs/directives.toml` `context.compaction_threshold_ratio`, default 0.9).
2. The compaction workflow preserves the recent raw tail verbatim, extracts must-keep chunks, and loads the `compactor` role through the existing llama.cpp router.
3. The compactor receives the bounded old context and emits the final compacted context; vector-store fallback is not a substitute for this compaction boundary.
4. The compactor emits durable structured memory, the workflow records a `context_compaction.v1` artifact, and Pi replaces the original context before calling the general role.
5. The compactor role has `warm_ttl_seconds=0`, so it unloads after the compaction workflow completes or fails.

Aliases and chainable forms:

```bash
pi /compact "what owns workflow truth?"
pi /start /ocr /compact
```

Design note: `/compact` is intentionally bounded: it loads the compactor only for the compaction boundary, then unloads it. The threshold and target ratio are policy fields in `configs/directives.toml`.
