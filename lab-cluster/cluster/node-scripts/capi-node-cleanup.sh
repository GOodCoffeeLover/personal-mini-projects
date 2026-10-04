#!/usr/bin/env bash
set -euxo pipefail

# kubeadm reset сам остановит kubelet и очистит
# /etc/kubernetes и /var/lib/kubelet
kubeadm reset -f --cleanup-tmp-dir

# kubeadm это сознательно не чистит
rm -rf /etc/cni/net.d
rm -rf /var/lib/cni
