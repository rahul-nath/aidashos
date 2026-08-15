# Pi Screenshot Text

Use this skill when a terminal user wants Pi to ingest a screenshot, image of text, or image with mixed visual/text content.

Canonical directive:

```bash
pi /screenshot /absolute/path/to/image.png optional natural-language question
```

Execution semantics:

1. Expand the directive to the local durable control plane, not to shell commands.
2. Load or reuse the OCR role for the image.
3. Store the image bytes as a durable `source_image` artifact.
4. OCR the image into normalized text.
5. Embed the OCR text into the current pgvector-compatible text store using the configured embedder role.
6. Preserve image artifact IDs in the store manifest so future image-native vectors can attach to the same durable records.
7. If natural-language text follows the image path, route that text to the default base model as a general query after the image has been stored.

Aliases and chainable forms:

```bash
pi /start /ocr /absolute/path/to/image.png optional question
pi /start /ocr /absolute/path/to/image.png && optional question
pi /store /absolute/path/to/image.png && optional question
```

Design note: current storage embeds OCR text and stores image artifacts. Future vision-only embeddings, such as V-JEPA-style image vectors, should attach to the stored image artifact IDs without changing the directive syntax.
