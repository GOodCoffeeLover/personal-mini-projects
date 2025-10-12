set -xe 
ARCH="arm64"
RELEASE="v1.31.6"
RELEASE_VERSION="v0.17.12"
CRICTL_VERSION="v1.31.1"
CONTAINERD_VERSION="1.7.25"
RUNC_VERSION="1.2.5"
CNI_PLUGINS_VERSION="v1.6.2"

curl -L "https://github.com/containerd/containerd/releases/download/v${CONTAINERD_VERSION}/containerd-${CONTAINERD_VERSION}-linux-${ARCH}.tar.gz" | sudo tar -C "/usr/local" -xz
mkdir -p "/etc/containerd/"
containerd config default \
| sed  "s/SystemdCgroup = false/SystemdCgroup = true/g"\
| sudo tee /etc/containerd/config.toml

CONTAINERD_SYSTEMD_DIR="/usr/local/lib/systemd/system/"
mkdir -p $CONTAINERD_SYSTEMD_DIR
curl -L -o "${CONTAINERD_SYSTEMD_DIR}/containerd.service" "https://raw.githubusercontent.com/containerd/containerd/main/containerd.service"
systemctl daemon-reload
systemctl enable --now containerd

curl -L -o runc "https://github.com/opencontainers/runc/releases/download/v${RUNC_VERSION}/runc.${ARCH}"
install -m 755 runc /usr/local/sbin/runc

DEST="/opt/cni/bin"
sudo mkdir -p "$DEST"
curl -L "https://github.com/containernetworking/plugins/releases/download/${CNI_PLUGINS_VERSION}/cni-plugins-linux-${ARCH}-${CNI_PLUGINS_VERSION}.tgz" | sudo tar -C "$DEST" -xz


DOWNLOAD_DIR="/usr/local/bin"
sudo mkdir -p "$DOWNLOAD_DIR"
curl -L "https://github.com/kubernetes-sigs/cri-tools/releases/download/${CRICTL_VERSION}/crictl-${CRICTL_VERSION}-linux-${ARCH}.tar.gz" | sudo tar -C $DOWNLOAD_DIR -xz

cd $DOWNLOAD_DIR
sudo curl -L --remote-name-all https://dl.k8s.io/release/${RELEASE}/bin/linux/${ARCH}/{kubeadm,kubelet,kubectl}
sudo chmod +x ${DOWNLOAD_DIR}/{kubeadm,kubelet,kubectl}

curl -sSL "https://raw.githubusercontent.com/kubernetes/release/${RELEASE_VERSION}/cmd/krel/templates/latest/kubelet/kubelet.service" | sed "s:/usr/bin:${DOWNLOAD_DIR}:g" | sudo tee /usr/lib/systemd/system/kubelet.service
sudo mkdir -p /usr/lib/systemd/system/kubelet.service.d
curl -sSL "https://raw.githubusercontent.com/kubernetes/release/${RELEASE_VERSION}/cmd/krel/templates/latest/kubeadm/10-kubeadm.conf" | sed "s:/usr/bin:${DOWNLOAD_DIR}:g" | sudo tee /usr/lib/systemd/system/kubelet.service.d/10-kubeadm.conf
sudo systemctl enable --now kubelet

cat <<EOF | sudo tee /etc/sysctl.d/k8s.conf
net.ipv4.ip_forward = 1
EOF

# Apply sysctl params without reboot
sudo sysctl --system

sudo apt update && \
sudo apt install  socat iptables iproute2 mount conntrack util-linux ethtool libc6 -y


swapoff -a
cat /etc/fstab | grep -v "swap.img" > /tmp/fstab 
mv /tmp/fstab /etc/fstab
