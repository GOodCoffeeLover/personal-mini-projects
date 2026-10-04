#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

command -v yq >/dev/null 2>&1 || { echo "Required command is missing: yq" >&2; exit 1; }

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${STATE_DIR:-${LAB_DIR}/.state}"
CLUSTER_DIR="${LAB_DIR}/cluster"
CAPI_DIR="${LAB_DIR}/capi"
APPLICATIONS_DIR="${LAB_DIR}/applications"
CLUSTER_NAME="$(yq -r '.metadata.name' "${CLUSTER_DIR}/cluster.yaml")"
[[ -n "$CLUSTER_NAME" && "$CLUSTER_NAME" != null ]] || { echo "Cluster name is missing" >&2; exit 1; }
KIND_NAME="${KIND_NAME:-${CLUSTER_NAME}-bootstrap}"
BOOTSTRAP_KUBECONFIG="${BOOTSTRAP_KUBECONFIG:-${STATE_DIR}/kind.kubeconfig}"
TARGET_KUBECONFIG="${TARGET_KUBECONFIG:-${STATE_DIR}/${CLUSTER_NAME}.kubeconfig}"
SSH_SECRET="${SSH_SECRET:-${LAB_DIR}/ssh-key-secret.yaml}"

require_command() {
  local command_name
  for command_name in "$@"; do
    command -v "$command_name" >/dev/null 2>&1 || {
      echo "Required command is missing: ${command_name}" >&2
      exit 1
    }
  done
}

wait_provider() {
  local kubeconfig="$1" kind="$2" name="$3" namespace="$4"
  kubectl --kubeconfig "$kubeconfig" -n "$namespace" wait \
    --for=condition=Ready "${kind}/${name}" --timeout=15m
}

assert_cluster_namespace() {
  local rendered="$1" invalid
  invalid="$(yq -r 'select(.kind != null and .metadata.namespace != strenv(CLUSTER_NAME)) | .kind + "/" + .metadata.name' "$rendered")"
  if [[ -n "$invalid" ]]; then
    echo "CAPI resources must be in namespace ${CLUSTER_NAME}:" >&2
    echo "$invalid" >&2
    exit 1
  fi
}

mkdir -p "$STATE_DIR"
export CLUSTER_NAME
