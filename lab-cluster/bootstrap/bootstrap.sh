#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/lib.sh"
require_command docker kind kubectl helm clusterctl yq ssh openssl

docker info >/dev/null
[[ -f "$SSH_SECRET" ]] || { echo "SSH Secret not found: ${SSH_SECRET}" >&2; exit 1; }
[[ "$(yq -r '.kind' "$SSH_SECRET")" == Secret ]] || { echo "Invalid SSH Secret" >&2; exit 1; }
[[ "$(yq -r '.metadata.name' "$SSH_SECRET")" == ssh-key-personal-git ]] || { echo "Unexpected SSH Secret name" >&2; exit 1; }
[[ "$(yq -r '.data.value // ""' "$SSH_SECRET")" != "" ]] || { echo "SSH Secret has no data.value" >&2; exit 1; }

key_file="$(mktemp "${STATE_DIR}/ssh-key.XXXXXX")"
chmod 0600 "$key_file"
trap 'rm -f "$key_file"' EXIT
yq -r '.data.value' "$SSH_SECRET" | openssl base64 -d -A >"$key_file"

for machine_file in "${CLUSTER_DIR}"/remote-machines/*.yaml; do
  [[ "$(yq -r '.kind' "$machine_file")" == PooledRemoteMachine ]] || continue
  address="$(yq -r '.spec.machine.address' "$machine_file")"
  user="$(yq -r '.spec.machine.user' "$machine_file")"
  port="$(yq -r '.spec.machine.port' "$machine_file")"
  echo "Checking SSH and passwordless sudo on ${address}:${port}"
  ssh -F /dev/null -o BatchMode=yes -o ConnectTimeout=8 \
    -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="${STATE_DIR}/known_hosts" \
    -i "$key_file" -p "$port" "${user}@${address}" 'sudo -n true'
done

rendered="$(mktemp "${STATE_DIR}/cluster.XXXXXX")"
trap 'rm -f "$key_file" "$rendered"' EXIT
kubectl kustomize "${LAB_DIR}/bootstrap/cluster" >"$rendered"
assert_cluster_namespace "$rendered"
[[ "$(yq -r 'select(.kind == "Cluster") | .metadata.name' "$rendered")" == "$CLUSTER_NAME" ]]
[[ "$(yq -r 'select(.kind == "PooledRemoteMachine") | .metadata.name' "$rendered" | wc -l | tr -d ' ')" == 3 ]]
kubectl kustomize "${CLUSTER_DIR}/addons/argo-cd" >"$rendered"
assert_cluster_namespace "$rendered"
kubectl kustomize "${CAPI_DIR}/providers" >/dev/null

if [[ -f "$TARGET_KUBECONFIG" ]] \
  && kubectl --kubeconfig "$TARGET_KUBECONFIG" -n "$CLUSTER_NAME" get "cluster/${CLUSTER_NAME}" >/dev/null 2>&1; then
  echo "The target already manages ${CLUSTER_NAME}; refusing to recreate it in kind." >&2
  exit 1
fi

if kind get clusters | grep -Fxq "$KIND_NAME"; then
  kind export kubeconfig --name "$KIND_NAME" --kubeconfig "$BOOTSTRAP_KUBECONFIG"
else
  kind create cluster --name "$KIND_NAME" --kubeconfig "$BOOTSTRAP_KUBECONFIG"
fi

# Prove that controller pods inside kind can reach the bare-metal SSH network.
kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" delete pod remote-network-check --ignore-not-found --wait=true >/dev/null
network_check='set -eu; for endpoint in'
for machine_file in "${CLUSTER_DIR}"/remote-machines/*.yaml; do
  [[ "$(yq -r '.kind' "$machine_file")" == PooledRemoteMachine ]] || continue
  network_check+=" $(yq -r '.spec.machine.address' "$machine_file"):$(yq -r '.spec.machine.port' "$machine_file")"
done
network_check+='; do nc -z -w 5 "${endpoint%:*}" "${endpoint##*:}"; done'
kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" run remote-network-check \
  --restart=Never --image=busybox:1.37 --command -- sh -ec "$network_check"
if ! kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" wait \
  --for=jsonpath='{.status.phase}'=Succeeded pod/remote-network-check --timeout=3m; then
  kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" describe pod remote-network-check >&2
  exit 1
fi
kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" delete pod remote-network-check --wait=true >/dev/null

helm repo add jetstack https://charts.jetstack.io --force-update
helm repo add capi-operator https://kubernetes-sigs.github.io/cluster-api-operator --force-update
helm repo update jetstack capi-operator
helm --kubeconfig "$BOOTSTRAP_KUBECONFIG" upgrade --install cert-manager jetstack/cert-manager \
  --version v1.21.2 --namespace cert-manager --create-namespace \
  --set crds.enabled=true --set crds.keep=true --wait --timeout 15m
helm --kubeconfig "$BOOTSTRAP_KUBECONFIG" upgrade --install capi-operator capi-operator/cluster-api-operator \
  --version 0.29.0 --namespace capi-operator-system --create-namespace \
  --values "${CAPI_DIR}/operator-values.yaml" --wait --timeout 20m

for crd in coreproviders.operator.cluster.x-k8s.io bootstrapproviders.operator.cluster.x-k8s.io \
  controlplaneproviders.operator.cluster.x-k8s.io infrastructureproviders.operator.cluster.x-k8s.io \
  addonproviders.operator.cluster.x-k8s.io; do
  kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" wait --for=condition=Established "crd/${crd}" --timeout=10m
done
kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" apply -k "${CAPI_DIR}/providers"

wait_provider "$BOOTSTRAP_KUBECONFIG" coreprovider cluster-api capi-system
wait_provider "$BOOTSTRAP_KUBECONFIG" bootstrapprovider kubeadm kubeadm-bootstrap-system
wait_provider "$BOOTSTRAP_KUBECONFIG" controlplaneprovider kubeadm kubeadm-control-plane-system
wait_provider "$BOOTSTRAP_KUBECONFIG" infrastructureprovider k0sproject-k0smotron k0sproject-k0smotron-infrastructure-system
wait_provider "$BOOTSTRAP_KUBECONFIG" addonprovider helm helm-addon-system

kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" create namespace "$CLUSTER_NAME" \
  --dry-run=client -o yaml | kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" apply -f -

# The private Secret remains untracked; only its transformed stream reaches Kubernetes.
yq '.metadata.namespace = strenv(CLUSTER_NAME) | .metadata.labels."clusterctl.cluster.x-k8s.io/move" = ""' \
  "$SSH_SECRET" | kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" apply -f -
kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" apply -k "${LAB_DIR}/bootstrap/cluster"

wait_provider "$BOOTSTRAP_KUBECONFIG" helmchartproxy lab-cluster-cilium "$CLUSTER_NAME"
clusterctl --kubeconfig "$BOOTSTRAP_KUBECONFIG" get kubeconfig "$CLUSTER_NAME" \
  --namespace "$CLUSTER_NAME" >"$TARGET_KUBECONFIG"
chmod 0600 "$TARGET_KUBECONFIG"
kubectl --kubeconfig "$TARGET_KUBECONFIG" wait --for=condition=Ready node --all --timeout=15m

kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" apply -k "${CLUSTER_DIR}/addons/argo-cd"
wait_provider "$BOOTSTRAP_KUBECONFIG" helmchartproxy lab-cluster-argo-cd "$CLUSTER_NAME"
kubectl --kubeconfig "$TARGET_KUBECONFIG" wait \
  --for=condition=Available deployment/argocd-server -n argocd --timeout=15m

echo "Workload cluster is ready; Argo CD will reconcile Applications from Git. Run ${LAB_DIR}/bootstrap/verify.sh before pivot."
