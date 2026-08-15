# Chandra OCR host-smoke fixtures

Put sanitized, non-private OCR images in this directory.
Use PNG, JPEG, or WebP for the first real-model acceptance set.
Add HEIC and TIFF fixtures to verify those formats against the installed llama.cpp decoder.

For every image, add a UTF-8 text file with the same stem and the suffix `.expected.txt`.
Put one required phrase on each non-empty line.
The host smoke normalizes whitespace and case, then requires every phrase to appear in Chandra's output.

Example:

```text
tests/fixtures/chandra_ocr/handwritten-shopping-list.png
tests/fixtures/chandra_ocr/handwritten-shopping-list.expected.txt
```

Run the real model check with:

```bash
LOCAL_AGENT_CHANDRA_HOST_SMOKE=1 uv run pytest -q tests/test_chandra_host_smoke.py
```

The test loads Chandra through the running llama.cpp router, checks that image input is advertised, OCRs every fixture, validates the expected phrases, and unloads Chandra if the test loaded it.
