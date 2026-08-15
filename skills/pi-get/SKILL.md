# Pi Get

Use this skill when a terminal user wants Pi to query the local pgvector store. `/get` is the durable retrieval directive.

Canonical directives:

```bash
pi /get workflowy durable boundary
pi /get "what owns workflow truth?"
```

Execution semantics:

1. `/get <natural-language query>` runs the durable `model_directive` workflow with `action=get`.
2. The workflow loads the embedder if it is not already warm, then queries pgvector for the top-k candidates.
3. The workflow records `directive_result.v1` with `ranked_ids` and top-k previews, so the query path is durable end-to-end.
4. `/get` never invokes the base general model. Pi prefers vector evidence for `/get`. Use plain text or `/start <query>` when you want a model-generated answer rather than retrieval.

Design note: `/get` is the safe path when the default model cannot load. It continues to work in degraded fallback mode because it only depends on the vector store and embedder.
