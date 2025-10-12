Установить argocd:
```bash
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
```

```bash
kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/refs/heads/master/deploy/local-path-storage.yaml
```
install registry-ui
```bash
helm upgrade --install docker-registry-ui joxit/docker-registry-ui --create-namespace -n registry-ui --set "ui.ingress.enabled=true,ui.ingress.host=registry-ui.k8s.deviant-optimist.home,ui.dockerRegistryUrl=http://registry.k8s.deviant-optimist.home,ui.deleteImages=true"
```

private registry:
```
> cat /etc/containerd/certs.d/registry.k8s.deviant-optimist.home/hosts.toml
server = "https://registry.k8s.deviant-optimist.home"

[host."http://registry.k8s.deviant-optimist.home"]
  skip_verify = true
  capabilities = ["pull", "resolve", "push"]
```
