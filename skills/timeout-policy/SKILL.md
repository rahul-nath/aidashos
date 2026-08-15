# Timeout Policy

Use this skill whenever adding or reviewing a blocking subprocess, HTTP call,
filesystem/object-store write, stream drain, ledger transition, or model call.

Before implementation, classify the operation with:

```bash
uv run local-agent timeout-policy --json
uv run local-agent timeout-policy http_application --expected-seconds 45 --json
```

Rules:

1. A timeout is a failure detector, not the expected happy-path duration.
2. Never reuse a model timeout for Git, health checks, or ledger commands.
3. Every retryable operation must first have an inner timeout; DBOS cannot retry
   a call that never returns or raises.
4. Use typed finite outcomes for timeout, retry exhaustion, cancellation, and
   recovery. Persist the failed attempt before starting a replacement.
5. A junior progress assessor is created only after deterministic stall
   detection. It is advisory and must not run for the lifetime of the job.
6. Subprocess tests must launch a real blocking or signal-resistant process.
   HTTP tests must use a server that accepts a connection and never responds.
7. Keep irreversible retries idempotent or fail closed for operator review.

Operation classes:

- `coordination`: one typed ledger command.
- `git`: status, diff, checkpoint, apply, or worktree management.
- `http_health`: idempotent readiness probe.
- `http_application`: bounded product/service request.
- `artifact_write`: local fsync/database insert or object-store upload.
- `stream_drain`: terminal stdout/stderr EOF collection.
- `progress_assessment`: on-demand junior advisory.
- `frontier_model`: long tool-using implementation or review.
