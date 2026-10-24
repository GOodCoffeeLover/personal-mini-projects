#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/lib.sh"
[[ -f "$SSH_SECRET" ]] || { echo "SSH Secret not found: ${SSH_SECRET}" >&2; exit 1; }

if [[ -f "$TARGET_KUBECONFIG" ]] \
  && kubectl --kubeconfig "$TARGET_KUBECONFIG" -n "$CLUSTER_NAME" get "cluster/${CLUSTER_NAME}" >/dev/null 2>&1; then
  echo "The target already manages ${CLUSTER_NAME}; refusing to recreate it in kind." >&2
  exit 1
fi

mkdir -p "$STATE_DIR" "$(dirname "$BOOTSTRAP_KUBECONFIG")" "$(dirname "$TARGET_KUBECONFIG")"

if kind get clusters | grep -Fxq "$KIND_NAME"; then
  kind export kubeconfig --name "$KIND_NAME" --kubeconfig "$BOOTSTRAP_KUBECONFIG"
else
  kind create cluster --name "$KIND_NAME" --kubeconfig "$BOOTSTRAP_KUBECONFIG"
fi
helm repo add jetstack https://charts.jetstack.io --force-update
helm repo add capi-operator https://kubernetes-sigs.github.io/cluster-api-operator --force-update
helm repo update jetstack capi-operator
helm --kubeconfig "$BOOTSTRAP_KUBECONFIG" upgrade --install cert-manager jetstack/cert-manager \
  --version v1.21.2 --namespace cert-manager --create-namespace \
  --set crds.enabled=true --set crds.keep=true --wait --timeout 15m
helm --kubeconfig "$BOOTSTRAP_KUBECONFIG" upgrade --install capi-operator capi-operator/cluster-api-operator \
  --version 0.30.0 --namespace capi-operator-system --create-namespace \
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
kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" apply -k "$CLUSTER_DIR"

wait_provider "$BOOTSTRAP_KUBECONFIG" helmchartproxy lab-cluster-cilium "$CLUSTER_NAME"
clusterctl --kubeconfig "$BOOTSTRAP_KUBECONFIG" get kubeconfig "$CLUSTER_NAME" \
  --namespace "$CLUSTER_NAME" >"$TARGET_KUBECONFIG"
chmod 0600 "$TARGET_KUBECONFIG"
kubectl --kubeconfig "$TARGET_KUBECONFIG" wait --for=condition=Ready node --all --timeout=15m

wait_provider "$BOOTSTRAP_KUBECONFIG" helmchartproxy lab-cluster-argo-cd "$CLUSTER_NAME"
kubectl --kubeconfig "$TARGET_KUBECONFIG" wait \
  --for=condition=Established crd/applications.argoproj.io --timeout=5m
kubectl --kubeconfig "$TARGET_KUBECONFIG" wait \
  --for=condition=Available deployment/argocd-server -n argocd --timeout=15m
kubectl --kubeconfig "$TARGET_KUBECONFIG" apply -f "${LAB_DIR}/applications/root.yaml"

echo "Workload cluster and Argo CD are ready; root Application applied. Argo CD will reconcile system Applications from Git."
echo "Keep lab-cluster-cluster unsynced until after pivot. Run ${LAB_DIR}/bootstrap/verify.sh pre-pivot when the target providers are ready."
