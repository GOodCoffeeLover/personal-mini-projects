#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/lib.sh"
require_command kubectl helm yq
mode="${1:-}"
[[ "$mode" == pre-pivot || "$mode" == post-pivot ]] || {
  echo "Usage: $0 pre-pivot|post-pivot" >&2
  exit 1
}
[[ -f "$TARGET_KUBECONFIG" ]] || { echo "Target kubeconfig not found: ${TARGET_KUBECONFIG}" >&2; exit 1; }

for app in lab-cluster-applications cert-manager capi-operator capi-providers lab-cluster-capi lab-cluster-remote-machines; do
  kubectl --kubeconfig "$TARGET_KUBECONFIG" -n argocd get "application/${app}" >/dev/null
done
for app in lab-cluster-capi lab-cluster-remote-machines; do
  automated="$(kubectl --kubeconfig "$TARGET_KUBECONFIG" -n argocd get "application/${app}" -o jsonpath='{.spec.syncPolicy.automated}')"
  [[ -z "$automated" ]] || { echo "Application ${app} must require manual sync" >&2; exit 1; }
done

kubectl --kubeconfig "$TARGET_KUBECONFIG" -n cert-manager wait \
  --for=condition=Available deployment/cert-manager --timeout=15m
for crd in coreproviders.operator.cluster.x-k8s.io bootstrapproviders.operator.cluster.x-k8s.io \
  controlplaneproviders.operator.cluster.x-k8s.io infrastructureproviders.operator.cluster.x-k8s.io \
  addonproviders.operator.cluster.x-k8s.io; do
  kubectl --kubeconfig "$TARGET_KUBECONFIG" wait --for=condition=Established "crd/${crd}" --timeout=10m
done
wait_provider "$TARGET_KUBECONFIG" coreprovider cluster-api capi-system
wait_provider "$TARGET_KUBECONFIG" bootstrapprovider kubeadm kubeadm-bootstrap-system
wait_provider "$TARGET_KUBECONFIG" controlplaneprovider kubeadm kubeadm-control-plane-system
wait_provider "$TARGET_KUBECONFIG" infrastructureprovider k0sproject-k0smotron k0sproject-k0smotron-infrastructure-system
wait_provider "$TARGET_KUBECONFIG" addonprovider helm helm-addon-system
kubectl --kubeconfig "$TARGET_KUBECONFIG" wait --for=condition=Ready node --all --timeout=15m
kubectl --kubeconfig "$TARGET_KUBECONFIG" -n argocd wait \
  --for=condition=Available deployment/argocd-server --timeout=15m

for release_spec in cilium:kube-system argocd:argocd; do
  release="${release_spec%%:*}"
  namespace="${release_spec#*:}"
  status="$(helm --kubeconfig "$TARGET_KUBECONFIG" -n "$namespace" status "$release" -o json | yq -p=json -r '.info.status')"
  [[ "$status" == deployed ]] || { echo "Helm release ${namespace}/${release} is ${status}" >&2; exit 1; }
done

if [[ "$mode" == pre-pivot ]]; then
  require_command clusterctl
  [[ -f "$BOOTSTRAP_KUBECONFIG" ]] || { echo "Bootstrap kubeconfig not found" >&2; exit 1; }
  kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" -n "$CLUSTER_NAME" get "cluster/${CLUSTER_NAME}" >/dev/null
  if kubectl --kubeconfig "$TARGET_KUBECONFIG" -n "$CLUSTER_NAME" get "cluster/${CLUSTER_NAME}" >/dev/null 2>&1; then
    echo "Cluster already exists in target; refusing pre-pivot check" >&2
    exit 1
  fi
  prm_count="$(kubectl --kubeconfig "$TARGET_KUBECONFIG" get pooledremotemachines -A -o json | yq -p=json -r '[.items[] | select(.metadata.namespace == strenv(CLUSTER_NAME))] | length')"
  [[ "$prm_count" == 0 ]] || { echo "PRM Application was synced before pivot" >&2; exit 1; }
  for proxy in lab-cluster-cilium lab-cluster-argo-cd; do
    strategy="$(kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" -n "$CLUSTER_NAME" get "helmchartproxy/${proxy}" -o jsonpath='{.spec.reconcileStrategy}')"
    [[ "$strategy" == InstallOnce ]] || { echo "Unsafe reconcile strategy for ${proxy}: ${strategy}" >&2; exit 1; }
  done
  source_prms="$(kubectl --kubeconfig "$BOOTSTRAP_KUBECONFIG" -n "$CLUSTER_NAME" get pooledremotemachines -o json)"
  [[ "$(yq -p=json -r '.items | length' <<<"$source_prms")" == 3 ]] || { echo "Expected three source PRM" >&2; exit 1; }
  [[ "$(yq -p=json -r '[.items[] | select(.metadata.labels."clusterctl.cluster.x-k8s.io/move" == "")] | length' <<<"$source_prms")" == 3 ]] || {
    echo "Every PRM must have the clusterctl move label" >&2
    exit 1
  }
  clusterctl move --dry-run --kubeconfig "$BOOTSTRAP_KUBECONFIG" \
    --to-kubeconfig "$TARGET_KUBECONFIG" --namespace "$CLUSTER_NAME" -v 4
  echo "Inspect the dry-run: all three PRM, required Secrets and both HelmChartProxy resources must be present."
else
  kubectl --kubeconfig "$TARGET_KUBECONFIG" -n "$CLUSTER_NAME" get "cluster/${CLUSTER_NAME}" >/dev/null
  kubectl --kubeconfig "$TARGET_KUBECONFIG" -n "$CLUSTER_NAME" get secret/ssh-key-personal-git >/dev/null
  prm_count="$(kubectl --kubeconfig "$TARGET_KUBECONFIG" -n "$CLUSTER_NAME" get pooledremotemachines -o name | wc -l | tr -d ' ')"
  [[ "$prm_count" == 3 ]] || { echo "Expected three moved PRM, found ${prm_count}" >&2; exit 1; }
  for proxy in lab-cluster-cilium lab-cluster-argo-cd; do
    strategy="$(kubectl --kubeconfig "$TARGET_KUBECONFIG" -n "$CLUSTER_NAME" get "helmchartproxy/${proxy}" -o jsonpath='{.spec.reconcileStrategy}')"
    [[ "$strategy" == InstallOnce ]] || { echo "Expected moved InstallOnce proxy: ${proxy}" >&2; exit 1; }
  done
  echo "Target cluster checks passed. Sync lab-cluster-remote-machines manually when ready."
fi
