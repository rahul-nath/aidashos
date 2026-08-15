# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS web-builder
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS app
WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    DBOS_APPLICATION_NAME=local-first-agent-os \
    LOCAL_AGENT_ENV=local \
    LOCAL_AGENT_SERVICE_NAME=local-agent \
    LOCAL_AGENT_STRUCTURED_LOGS=true \
    LOCAL_AGENT_OTEL_TRACES_ENABLED=false \
    LOCAL_AGENT_OTEL_TRACES_ENDPOINT=http://alloy:4318/v1/traces \
    LOCAL_AGENT_PYROSCOPE_ENABLED=false \
    LOCAL_AGENT_PYROSCOPE_SERVER_ADDRESS=http://pyroscope:4040 \
    LOCAL_AGENT_MEMORY_PROFILING_ENABLED=false \
    LOCAL_AGENT_WEB_DIST=/app/web/dist \
    LOCAL_AGENT_USE_DBOS=true \
    LOCAL_AGENT_MOCK_MODELS=true \
    LOCAL_AGENT_ARTIFACT_ROOT=/data/artifacts \
    LOCAL_AGENT_ARTIFACT_BACKEND=filesystem \
    LOCAL_AGENT_MINIO_ENDPOINT=minio:9000 \
    LOCAL_AGENT_MINIO_ACCESS_KEY=localagent \
    LOCAL_AGENT_MINIO_SECRET_KEY=localagent-secret \
    LOCAL_AGENT_MINIO_SECURE=false \
    LOCAL_AGENT_MINIO_ARTIFACT_BUCKET=local-agent-artifacts \
    LOCAL_AGENT_SPOOL_DIR=/data/spool \
    LOCAL_AGENT_CONFIG_DIR=/app/configs \
    LOCAL_AGENT_LLAMA_BASE_URL=http://host.docker.internal:8080 \
    LOCAL_AGENT_BOOTSTRAP_VECTOR_STORE=true \
    LOCAL_AGENT_REQUIRE_VECTOR_STORE=false

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY configs/ ./configs/
COPY migrations/ ./migrations/
COPY skills/ ./skills/
COPY dbos-config.yaml ./
COPY scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh
COPY --from=web-builder /app/web/dist ./web/dist

RUN uv sync --frozen --no-dev && chmod +x ./scripts/docker-entrypoint.sh

EXPOSE 8000 3001

ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
CMD ["local-agent", "serve", "--host", "0.0.0.0", "--port", "8000"]
