# Pi Send-to-Workflowy

Use this skill when the user wants Pi to ingest a single local file (text, audio, or image) and append the processed result to Workflowy under a top-level `MM/DD` bullet that lives next to (not inside) the user's `/done` bullet.

Canonical directive:

```bash
pi /send-to-wf /absolute/path/to/file 04/28
```

Execution semantics:

1. Parse the directive as `/send-to-wf <path> <MM/DD>`. The MM/DD argument is required and validates against `01/01..12/31`.
2. Determine the file kind by extension:
   - `.txt`, `.md`, and other configured `text_extensions` — read the file directly.
   - `.mp3`, `.m4a`, `.wav`, and other configured `audio_extensions` — route through the durable `audio_transcription` workflow, which delegates to the centralized registry-backed `AudioTranscriber` loader (`src/local_first_agent_os/audio_transcriber.py`). The transcript text is what gets posted to Workflowy. Non-mock runs require the separate whisper.cpp server from `scripts/start-whisper.sh`.
   - `.png`, `.jpg`, and other configured `image_extensions` — call the default base model (gemma4) with a JSON-schema prompt to decide `is_text`. If true, run `/ocr` over the same image. If false, capture the model's two-sentence description. The image bytes themselves are imported as a durable `source_image` artifact (Postgres metadata + content-addressed file under `LOCAL_AGENT_ARTIFACT_ROOT`); only the text result is sent to Workflowy. The payload's `downstream` block includes the artifact id, artifact URI, and SHA so the Workflowy entry can be linked back to the durable image record.
3. Persist the processed content as a `send_to_wf_payload.v1` artifact alongside the source artifact and any classification/OCR artifacts.
4. Call the durable `workflowy_day_bullet_insert` tool, which uses the documented Workflowy v1 API. It calls `GET https://workflowy.com/api/v1/nodes?parent_id=None` for the top-level listing (instead of the rate-limited export endpoint), identifies the `/done` top-level node, and looks for an existing top-level sibling whose name is the same `MM/DD`. If one exists, its `id` is reused; otherwise a new top-level `MM/DD` bullet is created via `POST /api/v1/nodes` with `parent_id="None"` and `position="top"` (next to `/done`).
5. The processed content is then inserted as a child of that `MM/DD` bullet via `POST /api/v1/nodes` with the parent's `id`. Authentication uses `Authorization: Bearer $WF_API_KEY`. Without `WF_API_KEY` (or with `LOCAL_AGENT_WORKFLOWY_DRY_RUN=true`), the tool returns a dry-run payload instead of calling the API. The top-level listing is cached for 30s to avoid hammering the listing endpoint when a chain of `/send-to-wf` calls runs back-to-back.

Permission gates:

- `pi.permission_gate.workflowy_write.v1` — Workflowy writes default to dry-run. Set `LOCAL_AGENT_WORKFLOWY_DRY_RUN=false` and provide `LOCAL_AGENT_WORKFLOWY_INSERT_SCRIPT` to enable real writes.
- `pi.permission_gate.send_to_wf_filesystem.v1` — the source path must exist and have an extension that matches the configured text/audio/image lists.

Design note: the directive intentionally never adds anything inside the user's `/done` bullet. The `MM/DD` bullet is always created at the same depth as `/done`, so the user's review pipeline stays intact.
