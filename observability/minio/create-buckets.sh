#!/usr/bin/env sh
set -eu

until mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"; do
  sleep 1
done

for bucket in \
  local-agent-artifacts \
  local-agent-loki \
  local-agent-tempo \
  local-agent-pyroscope \
  local-agent-log-archive; do
  mc mb --ignore-existing "local/$bucket"
done
