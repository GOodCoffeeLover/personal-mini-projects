set -Eeuo pipefail

KUBE_VIP_VERSION="v1.2.3"

is_ipv4_address() {
  local address="$1"
  local -a octets
  IFS=. read -r -a octets <<<"$address"
  (( ${#octets[@]} == 4 )) || return 1

  local octet
  for octet in "${octets[@]}"; do
    [[ "$octet" =~ ^[0-9]{1,3}$ ]] || return 1
    (( 10#$octet <= 255 )) || return 1
  done
}

case "$#" in
  0)
    # A reused host can retain the other phase's config; prefer the newer file.
    if [[ -f /run/kubeadm/kubeadm-join-config.yaml && ( ! -f /run/kubeadm/kubeadm.yaml \
      || /run/kubeadm/kubeadm-join-config.yaml -nt /run/kubeadm/kubeadm.yaml ) ]]; then
      KUBEADM_CONFIG="/run/kubeadm/kubeadm-join-config.yaml"
      endpoint_key="apiServerEndpoint:"
      kubeconfig="/etc/kubernetes/admin.conf"
    elif [[ -f /run/kubeadm/kubeadm.yaml ]]; then
      KUBEADM_CONFIG="/run/kubeadm/kubeadm.yaml"
      endpoint_key="controlPlaneEndpoint:"
      kubeconfig="/etc/kubernetes/super-admin.conf"
    else
      echo "Kubeadm init/join config not found; pass an endpoint and kubeconfig explicitly" >&2
      exit 1
    fi
    endpoint="$(
      awk -v key="$endpoint_key" '$1 == key { value=$2; gsub(/^"|"$/, "", value); print value; exit }' \
        "$KUBEADM_CONFIG"
    )"
    # Once kubeadm has completed, use its regular admin kubeconfig.
    if [[ -f /etc/kubernetes/admin.conf && -s /etc/kubernetes/admin.conf ]]; then
      kubeconfig="/etc/kubernetes/admin.conf"
    fi
    ;;
  2)
    endpoint="$1"
    kubeconfig="$2"
    ;;
  *)
    echo "Usage: $0 [<IPv4:port> <absolute-kubeconfig-path>]" >&2
    exit 1
    ;;
esac

[[ "$kubeconfig" == /* ]] || { echo "Kubeconfig path must be absolute" >&2; exit 1; }

vip_address="${endpoint%:*}"
vip_port="${endpoint##*:}"
if [[ "$vip_address" == "$endpoint" ]] \
  || ! is_ipv4_address "$vip_address" \
  || [[ ! "$vip_port" =~ ^[0-9]+$ ]] \
  || (( 10#$vip_port < 1 || 10#$vip_port > 65535 )); then
  echo "Invalid kube-vip endpoint '${endpoint}': expected an IPv4 address with a valid port" >&2
  exit 1
fi

TEMP_DIR="$(mktemp -d /run/setup-kube-vip.XXXXXX)"
trap 'rm -rf -- "${TEMP_DIR:?}"' EXIT

cat >"${TEMP_DIR}/kube-vip.yaml" <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: kube-vip
  namespace: kube-system
  labels:
    app.kubernetes.io/name: kube-vip
spec:
  terminationGracePeriodSeconds: 60
  containers:
  - name: kube-vip
    image: ghcr.io/kube-vip/kube-vip:${KUBE_VIP_VERSION}
    imagePullPolicy: IfNotPresent
    args:
    - manager
    env:
    - name: address
      value: "${vip_address}"
    - name: port
      value: "${vip_port}"
    - name: vip_arp
      value: "true"
    - name: vip_cidr
      value: "32"
    - name: vip_interface
      value: ""
    - name: cp_enable
      value: "true"
    - name: cp_namespace
      value: kube-system
    - name: vip_leaderelection
      value: "true"
    - name: vip_leasename
      value: plndr-cp-lock
    - name: vip_leaseduration
      value: "15"
    - name: vip_renewdeadline
      value: "10"
    - name: vip_retryperiod
      value: "2"
    - name: svc_enable
      value: "false"
    - name: k8s_config_file
      value: "${kubeconfig}"
    - name: kubernetes_addr
      value: "kubernetes:${vip_port}"
    securityContext:
      capabilities:
        add:
        - NET_ADMIN
        - NET_RAW
    volumeMounts:
    - name: kubeconfig
      mountPath: "${kubeconfig}"
      readOnly: true
  hostAliases:
  - ip: 127.0.0.1
    hostnames:
    - kubernetes
  hostNetwork: true
  priorityClassName: system-node-critical
  volumes:
  - name: kubeconfig
    hostPath:
      path: "${kubeconfig}"
      type: File
EOF

sudo mkdir -p /etc/kubernetes/manifests
if [[ ! -f /etc/kubernetes/manifests/kube-vip.yaml ]] \
  || ! sudo cmp -s "${TEMP_DIR}/kube-vip.yaml" /etc/kubernetes/manifests/kube-vip.yaml; then
  sudo install -m 0644 "${TEMP_DIR}/kube-vip.yaml" /etc/kubernetes/manifests/kube-vip.yaml
fi
