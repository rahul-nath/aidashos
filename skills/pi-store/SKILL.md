# Pi Store

Use this skill when a terminal user wants Pi to embed text and OCR images from a local file or directory into the durable pgvector store. `/store` is the durable ingestion directive.

Canonical directives:

```bash
pi /store /absolute/path/to/directory
pi /store /absolute/path/to/file.md
pi /start /store /absolute/path/to/directory
pi /store /remote /absolute/path/to/directory
pi /embed /absolute/path/to/directory
```

Execution semantics:

1. `/store <path>` resolves to the durable `directory_embedding` workflow. `/start /store <path>` and `/embed <path>` are aliases for the same boundary.
2. The workflow loads the embedder; for image files it also loads the OCR and general models so screenshots are stored as durable artifacts and embedded as OCR text.
3. Text files matching `configs/directives.toml` `store.text_extensions` are read, written as `normalized_text` artifacts, and embedded.
4. Image files matching `store.image_extensions` are imported as `source_image` artifacts, sent through OCR, and the OCR text is embedded. The store manifest keeps `image_artifacts` so future image-native vector models can attach to the same durable records without changing directive syntax.
5. Files larger than `store.max_file_bytes` are skipped with a recorded counter.
6. `/remote` is reserved as a flag for future remote-target ingestion. The current implementation embeds locally and records `remote_requested` in the manifest.
7. The directive fails hard with a `FAILED_PERMANENT` workflow status if the path does not exist or the directory contains no supported text or image files.

Aliases and chainable forms:

```bash
pi /store /absolute/path/to/dir /get "what was stored?"
pi /screenshot /absolute/path/to/image.png "what is in this image?"
```

Design note: `/store` never embeds remote content silently. Remote ingestion is gated behind `/remote` so a path mistake never reaches a remote ledger. A future container-side init step replays a vector-store dump or invokes `/store` against a host-side directory; both paths flow through this same durable boundary.
