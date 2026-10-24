#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${STATE_DIR:-${LAB_DIR}/.state}"
CLUSTER_DIR="${LAB_DIR}/cluster"
CAPI_DIR="${LAB_DIR}/capi"
CLUSTER_NAME="$(yq -r '.metadata.name' "${CLUSTER_DIR}/cluster.yaml")"
KIND_NAME="${KIND_NAME:-${CLUSTER_NAME}-bootstrap}"
BOOTSTRAP_KUBECONFIG="${BOOTSTRAP_KUBECONFIG:-${STATE_DIR}/kind.kubeconfig}"
TARGET_KUBECONFIG="${TARGET_KUBECONFIG:-${STATE_DIR}/${CLUSTER_NAME}.kubeconfig}"
SSH_SECRET="${SSH_SECRET:-${CLUSTER_DIR}/remote-machines/ssh-key-secret.yaml}"

wait_provider() {
  local kubeconfig="$1" kind="$2" name="$3" namespace="$4"
  kubectl --kubeconfig "$kubeconfig" -n "$namespace" wait \
    --for=condition=Ready "${kind}/${name}" --timeout=15m
}

export CLUSTER_NAME
