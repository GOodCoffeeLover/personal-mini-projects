#!/usr/bin/env bash
set -euxo pipefail

# Дать kubelet штатно остановить kube-vip и освободить VIP до reset.
rm -f /etc/kubernetes/manifests/kube-vip.yaml
deadline=$((SECONDS + 90))
while true; do
  containers="$(crictl ps --name '^kube-vip$' -q)"
  [[ -z "$containers" ]] && break
  if (( SECONDS >= deadline )); then
    echo "Timed out waiting for kube-vip to stop; kubeadm reset was not started" >&2
    exit 1
  fi
  sleep 1
done

# kubeadm reset сам остановит kubelet и очистит
# /etc/kubernetes и /var/lib/kubelet
kubeadm reset -f --cleanup-tmp-dir

# kubeadm это сознательно не чистит
rm -rf /etc/cni/net.d
rm -rf /var/lib/cni
rm -rf /run/kubeadm /run/cluster-api
