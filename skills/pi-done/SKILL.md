# Pi Done

Use this skill when a terminal user wants Pi to search local embeddings and aggregate the results through the default base model. `/done` is the durable retrieval-plus-aggregation directive.

Canonical directive:

```bash
pi /done embeddings durability
pi /done "what owns workflow truth?"
```

Execution semantics:

1. Everything after `/done` is treated as a free-form search query. Quote the query if it contains special shell characters.
2. The workflow runs the durable `done_recall` boundary: pgvector search, then a single base-model call that aggregates the top snippets into a concise answer.
3. The aggregated answer, the ranked chunk ids, and per-snippet previews are persisted as a `done_recall_result.v1` artifact so future calls can audit the recall path.
4. If the default base model is in general fallback mode, `/done` returns vector-store snippets without a model-aggregated answer and records `fallback_active=true`.
5. `/done` is intentionally distinct from the user's Workflowy `/done` bullet. The directive never reads or writes Workflowy by itself; it only consumes the local pgvector store.

Aliases and chainable forms:

```bash
pi /start /done "what owns workflow truth?"
pi /store /Users/rahul/notes /done "what was just stored?"
```

Design note: `/done` differs from `/get` by adding base-model aggregation on top of the vector-store candidates. `/get` returns ranked previews; `/done` returns a synthesized answer plus the ranked previews. Both share the same retrieval boundary, so embeddings only have to be loaded once.
