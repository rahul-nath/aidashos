#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CLUSTER_NAME="${KIND_CLUSTER_NAME:-local-agent}"
IMAGE_TAG="${IMAGE_TAG:-local-first-agent-os:latest}"
NAMESPACE="${K8S_NAMESPACE:-local-first-agent-os}"

if ! command -v kind >/dev/null 2>&1; then
  echo "kind not found on PATH." >&2
  exit 127
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl not found on PATH." >&2
  exit 127
fi

if ! kind get clusters | grep -qx "$CLUSTER_NAME"; then
  kind create cluster --name "$CLUSTER_NAME"
fi

docker build -t "$IMAGE_TAG" .
kind load docker-image "$IMAGE_TAG" --name "$CLUSTER_NAME"
if [[ -n "${DBOS_CONDUCTOR_KEY:-}" ]]; then
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n "$NAMESPACE" create secret generic dbos-conductor \
    --from-literal=DBOS_CONDUCTOR_KEY="$DBOS_CONDUCTOR_KEY" \
    --dry-run=client \
    -o yaml | kubectl apply -f -
else
  echo "DBOS_CONDUCTOR_KEY is not set; deploying without DBOS Conductor." >&2
fi
kubectl apply -k k8s/kind
kubectl -n "$NAMESPACE" rollout status deployment/postgres --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/local-agent-app --timeout=180s

echo "Run: kubectl -n $NAMESPACE port-forward svc/local-agent-app 8000:8000"
echo "Open: http://127.0.0.1:8000"
