set -Eeuo pipefail

case "$(uname -m)" in
  x86_64)
    ARCH="amd64"
    ;;
  aarch64 | arm64)
    ARCH="arm64"
    ;;
  *)
    echo "Unsupported architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

RELEASE="${RELEASE:-v1.37.0}"
RELEASE="${RELEASE#v}"
if [[ "$RELEASE" =~ ^([0-9]+)\.([0-9]+)\.[0-9]+([+-].*)?$ ]]; then
  CRICTL_VERSION="v${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.0"
  RELEASE="v${RELEASE}"
else
  echo "Unsupported Kubernetes version: ${RELEASE}" >&2
  exit 1
fi

RELEASE_VERSION="v0.21.1"
CONTAINERD_VERSION="2.3.5"
RUNC_VERSION="1.5.1"
CNI_PLUGINS_VERSION="v1.9.1"

command_has_version() {
  local expected_version="$1"
  shift

  local version_output
  if ! version_output="$("$@" 2>&1)"; then
    return 1
  fi

  [[ "$version_output" == *"$expected_version"* ]]
}

TEMP_DIR="$(mktemp -d /run/setup-kube.XXXXXX)"
trap 'rm -rf -- "${TEMP_DIR:?}"' EXIT

containerd_changed=false
if ! command_has_version "$CONTAINERD_VERSION" containerd --version; then
  curl --fail --location "https://github.com/containerd/containerd/releases/download/v${CONTAINERD_VERSION}/containerd-${CONTAINERD_VERSION}-linux-${ARCH}.tar.gz" \
    | sudo tar -C /usr/local -xz
  containerd_changed=true
fi

sudo mkdir -p /etc/containerd
if [[ ! -f /etc/containerd/config.toml ]]; then
  containerd config default \
    | sed "s/SystemdCgroup = false/SystemdCgroup = true/g" \
    | sudo tee /etc/containerd/config.toml >/dev/null
  containerd_changed=true
elif grep -q "SystemdCgroup = false" /etc/containerd/config.toml; then
  sudo sed -i "s/SystemdCgroup = false/SystemdCgroup = true/g" /etc/containerd/config.toml
  containerd_changed=true
fi

CONTAINERD_SYSTEMD_DIR="/usr/local/lib/systemd/system"
sudo mkdir -p "$CONTAINERD_SYSTEMD_DIR"
if [[ ! -f "${CONTAINERD_SYSTEMD_DIR}/containerd.service" ]]; then
  sudo curl --fail --location \
    --output "${CONTAINERD_SYSTEMD_DIR}/containerd.service" \
    "https://raw.githubusercontent.com/containerd/containerd/main/containerd.service"
  containerd_changed=true
fi

sudo systemctl daemon-reload
sudo systemctl enable containerd
if [[ "$containerd_changed" == true ]]; then
  sudo systemctl restart containerd
else
  sudo systemctl start containerd
fi

if ! command_has_version "$RUNC_VERSION" /usr/local/sbin/runc --version; then
  curl --fail --location \
    --output "${TEMP_DIR}/runc" \
    "https://github.com/opencontainers/runc/releases/download/v${RUNC_VERSION}/runc.${ARCH}"
  sudo install -m 0755 "${TEMP_DIR}/runc" /usr/local/sbin/runc
fi

DEST="/opt/cni/bin"
sudo mkdir -p "$DEST"
if ! command_has_version "${CNI_PLUGINS_VERSION#v}" "${DEST}/bridge" --version; then
  curl --fail --location "https://github.com/containernetworking/plugins/releases/download/${CNI_PLUGINS_VERSION}/cni-plugins-linux-${ARCH}-${CNI_PLUGINS_VERSION}.tgz" \
    | sudo tar -C "$DEST" -xz
fi

DOWNLOAD_DIR="/usr/local/bin"
sudo mkdir -p "$DOWNLOAD_DIR"
if ! command_has_version "${CRICTL_VERSION#v}" "${DOWNLOAD_DIR}/crictl" --version; then
  curl --fail --location "https://github.com/kubernetes-sigs/cri-tools/releases/download/${CRICTL_VERSION}/crictl-${CRICTL_VERSION}-linux-${ARCH}.tar.gz" \
    | sudo tar -C "$DOWNLOAD_DIR" -xz
fi

kubelet_changed=false
for binary in kubeadm kubelet kubectl; do
  case "$binary" in
    kubeadm)
      version_command=("${DOWNLOAD_DIR}/${binary}" version -o short)
      ;;
    kubelet)
      version_command=("${DOWNLOAD_DIR}/${binary}" --version)
      ;;
    kubectl)
      version_command=("${DOWNLOAD_DIR}/${binary}" version --client=true)
      ;;
  esac

  if ! command_has_version "$RELEASE" "${version_command[@]}"; then
    curl --fail --location \
      --output "${TEMP_DIR}/${binary}" \
      "https://dl.k8s.io/release/${RELEASE}/bin/linux/${ARCH}/${binary}"
    sudo install -m 0755 "${TEMP_DIR}/${binary}" "${DOWNLOAD_DIR}/${binary}"
    if [[ "$binary" == kubelet ]]; then
      kubelet_changed=true
    fi
  fi
done

if [[ ! -f /usr/lib/systemd/system/kubelet.service ]]; then
  curl --fail --silent --show-error --location \
    "https://raw.githubusercontent.com/kubernetes/release/${RELEASE_VERSION}/cmd/krel/templates/latest/kubelet/kubelet.service" \
    | sed "s:/usr/bin:${DOWNLOAD_DIR}:g" \
    | sudo tee /usr/lib/systemd/system/kubelet.service >/dev/null
  kubelet_changed=true
fi

sudo mkdir -p /usr/lib/systemd/system/kubelet.service.d
if [[ ! -f /usr/lib/systemd/system/kubelet.service.d/10-kubeadm.conf ]]; then
  curl --fail --silent --show-error --location \
    "https://raw.githubusercontent.com/kubernetes/release/${RELEASE_VERSION}/cmd/krel/templates/latest/kubeadm/10-kubeadm.conf" \
    | sed "s:/usr/bin:${DOWNLOAD_DIR}:g" \
    | sudo tee /usr/lib/systemd/system/kubelet.service.d/10-kubeadm.conf >/dev/null
  kubelet_changed=true
fi

sudo systemctl daemon-reload
sudo systemctl enable kubelet
if [[ "$kubelet_changed" == true ]]; then
  sudo systemctl restart kubelet
else
  sudo systemctl start kubelet
fi

SYSCTL_CONFIG="net.ipv4.ip_forward = 1"
if [[ ! -f /etc/sysctl.d/k8s.conf ]] || [[ "$(</etc/sysctl.d/k8s.conf)" != "$SYSCTL_CONFIG" ]]; then
  printf '%s\n' "$SYSCTL_CONFIG" | sudo tee /etc/sysctl.d/k8s.conf >/dev/null
  sudo sysctl --system
fi

required_packages=(socat iptables iproute2 mount conntrack util-linux ethtool libc6)
missing_packages=()
for package in "${required_packages[@]}"; do
  if [[ "$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)" != "install ok installed" ]]; then
    missing_packages+=("$package")
  fi
done

if (( ${#missing_packages[@]} > 0 )); then
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing_packages[@]}"
fi

sudo swapoff -a
if grep -q 'swap\.img' /etc/fstab; then
  sudo sed -i '/swap\.img/d' /etc/fstab
fi
