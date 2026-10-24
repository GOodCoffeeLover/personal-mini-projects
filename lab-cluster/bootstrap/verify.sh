#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/lib.sh"
mode="${1:-}"
case "$mode" in
  pre-pivot) management_kubeconfig="$BOOTSTRAP_KUBECONFIG" ;;
  post-pivot) management_kubeconfig="$TARGET_KUBECONFIG" ;;
  *) echo "Usage: $0 pre-pivot|post-pivot" >&2; exit 1 ;;
esac

kubectl --kubeconfig "$TARGET_KUBECONFIG" -n argocd get applications \
  lab-cluster-applications cert-manager capi-operator capi-providers lab-cluster-cluster cilium argocd

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
kubectl --kubeconfig "$TARGET_KUBECONFIG" -n kube-system rollout status daemonset/cilium --timeout=15m
kubectl --kubeconfig "$TARGET_KUBECONFIG" -n kube-system rollout status deployment/cilium-operator --timeout=15m
kubectl --kubeconfig "$TARGET_KUBECONFIG" -n argocd rollout status deployment/argocd-server --timeout=15m
kubectl --kubeconfig "$TARGET_KUBECONFIG" -n argocd rollout status statefulset/argocd-application-controller --timeout=15m
kubectl --kubeconfig "$TARGET_KUBECONFIG" -n argocd rollout status deployment/argocd-repo-server --timeout=15m
kubectl --kubeconfig "$TARGET_KUBECONFIG" -n argocd rollout status deployment/argocd-redis --timeout=15m
echo "Helm status describes the bootstrap releases; after Argo CD takeover, inspect Applications and live workloads for current state."
helm --kubeconfig "$TARGET_KUBECONFIG" -n kube-system status cilium
helm --kubeconfig "$TARGET_KUBECONFIG" -n argocd status argocd

# Only resource metadata is printed; Secret contents stay out of the output.
kubectl --kubeconfig "$management_kubeconfig" -n "$CLUSTER_NAME" get \
  clusters,clusterclasses,machines,pooledremotemachines,helmchartproxies,helmreleaseproxies,secrets

if [[ "$mode" == pre-pivot ]]; then
  echo "Target Cluster and PRM lists must be empty before pivot:"
  kubectl --kubeconfig "$TARGET_KUBECONFIG" -n "$CLUSTER_NAME" get clusters,pooledremotemachines
  clusterctl move --dry-run --kubeconfig "$BOOTSTRAP_KUBECONFIG" \
    --to-kubeconfig "$TARGET_KUBECONFIG" --namespace "$CLUSTER_NAME" -v 4
  echo "Review the dry-run for Machines, templates, required Secrets, three PRM and both addon proxies with their releases before moving."
else
  echo "Review the moved resources, Applications and live workloads, then inspect the lab-cluster-cluster diff before a manual sync."
fi
