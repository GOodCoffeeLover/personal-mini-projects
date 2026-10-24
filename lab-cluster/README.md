# Homelab cluster bootstrap

`lab-cluster` создаётся на трёх физических машинах через временный kind management-кластер. Сначала работает один control-plane узел, выбранный k0smotron из пула. Адрес API через kube-vip — `10.60.80.1:6443`; Cilium заменяет kube-proxy. План и критерии готовности: [PLAN.md](PLAN.md).

## Каталоги

| Путь | Назначение |
| --- | --- |
| `capi/operator-values.yaml` | Значения Helm chart CAPI Operator без установки провайдеров |
| `capi/providers/` | Пять отдельных Provider CR и namespace их контроллеров |
| `cluster/` | Общая сборка Cluster, ClusterClass, шаблонов, скриптов, трёх PRM и обоих аддонов |
| `cluster/addons/` | Cilium и Argo CD `HelmChartProxy`; chart Argo CD создаёт корневое Application через `extraObjects` |
| `cluster/remote-machines/` | Три `PooledRemoteMachine`; локальный SSH Secret не входит в сборку |
| `applications/` | Корневое Application, системные Applications и ручное `lab-cluster-cluster` |
| `bootstrap/bootstrap.sh` | Создание kind и начальная установка кластера |
| `bootstrap/verify.sh` | Проверка перед pivot и после него; не меняет ресурсы |

Все CAPI-ресурсы конкретного кластера находятся в namespace `lab-cluster`, совпадающем с `Cluster.metadata.name`. Контроллеры и Argo CD работают в собственных namespace.

## Подготовка

Нужны Docker, kind `v0.33.0`, kubectl `v1.36.5`, Helm 3, clusterctl `v1.14.3`, Mike Farah yq 4, OpenSSL и SSH. Образ kind должен использовать Kubernetes `1.33–1.37`, поддерживаемый CAPI 1.14. Узлы — Debian/Ubuntu с systemd, `curl`, SSH и доступным `sudo -n`. Скрипты скачивают закреплённые версии компонентов, пропуская уже установленные версии и файлы.

Создайте локальный `lab-cluster/cluster/remote-machines/ssh-key-secret.yaml`, исключённый из Git: Kubernetes Secret с `metadata.name: ssh-key-personal-git` и `data.value`, содержащим base64 приватного SSH-ключа. Bootstrap проставит ему namespace `lab-cluster` и метку `clusterctl.cluster.x-k8s.io/move`. Secret применяется отдельно и не нужен для Kustomize или Argo CD. Ключ и kubeconfig не добавляйте в Git.

Проверки окружения выполняются вручную. Сначала проверьте локальные инструменты:

```bash
docker info
kind version
kubectl version --client
helm version --short
clusterctl version -o short
yq --version
```

Проверьте SSH и sudo на всех трёх машинах ключом, соответствующим Secret; при необходимости измените `SSH_KEY`:

```bash
SSH_KEY="$HOME/.ssh/personal_git"
for address in 10.60.80.101 10.60.80.102 10.60.80.103; do
  ssh -o StrictHostKeychecking=no -o BatchMode=yes -o ConnectTimeout=8 -i "$SSH_KEY" "schizo@${address}" \
    'sudo -n true && cat /etc/os-release && ip -br -4 address && ip route get 10.60.80.1'
done
```

Если sudo требует пароль, то выполнить:
```
echo "$USER ALL=(ALL:ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/$USER
```

Узлы должны находиться в общей L2-сети для kube-vip; VIP `10.60.80.1` должен быть свободен и исключён из DHCP-пула. С узла в этой сети можно проверить конфликт адреса командой `sudo arping -D -c 3 10.60.80.1` при установленном `arping`. Также проверьте доступ узлов к GitHub, `dl.k8s.io` и реестрам образов.

Чтобы заранее проверить доступ к SSH из pod в kind, создайте временный management-кластер и выполните сетевую проверку. Bootstrap затем использует этот же kind:

```bash
mkdir -p lab-cluster/.state
kind create cluster --name lab-cluster-bootstrap --kubeconfig lab-cluster/.state/kind.kubeconfig
kubectl --kubeconfig lab-cluster/.state/kind.kubeconfig run remote-network-check \
  --restart=Never --image=busybox:1.37 --rm -i --command -- sh -ec \
  'for host in 10.60.80.101 10.60.80.102 10.60.80.103; do nc -z -w 5 "$host" 22; done'
```

Если kind уже существует, пропустите его создание. При переопределении имени или kubeconfig используйте те же значения в этих командах и bootstrap.

До запуска опубликуйте эту конфигурацию в `main` репозитория `https://github.com/GOodCoffeeLover/personal-mini-projects.git`: Argo CD читает именно опубликованные манифесты.

## Bootstrap

Из корня репозитория выполните:

```bash
lab-cluster/bootstrap/bootstrap.sh
```

Скрипт создаёт или использует существующий kind, устанавливает cert-manager и CAPI Operator через Helm, применяет пять Provider CR и ждёт их готовности. Затем отдельно применяет SSH Secret и **одну** Kustomize-сборку `cluster/` с Cluster, тремя PRM и обоими HelmChartProxy. CAAPH устанавливает Cilium и Argo CD; скрипт ждёт Cilium, Ready узла и Argo CD. Chart Argo CD создаёт корневое Application, которое автоматически создаёт остальные Applications из Git.

В целевом кластере cert-manager, CAPI Operator и Provider CR поддерживаются Argo CD автоматически. `lab-cluster-cluster` охватывает Cluster, пул машин и аддоны, но синхронизируется только вручную, без автоматического prune и каскадного finalizer. **До pivot не синхронизируйте это Application:** Cluster и PRM должны попасть в целевой management-кластер через move.

Kubeconfig хранятся по умолчанию в `lab-cluster/.state/kind.kubeconfig` и `lab-cluster/.state/lab-cluster.kubeconfig` (каталог исключён из Git). Пути можно изменить переменными `STATE_DIR`, `BOOTSTRAP_KUBECONFIG`, `TARGET_KUBECONFIG`, `SSH_SECRET`; имя kind — переменной `KIND_NAME`. При последующих вызовах verify и move используйте те же значения. Bootstrap предназначен для этапа до pivot; существующий управляемый кластер повторно создавать не нужно.

## Ручной pivot

Дождитесь автоматической установки всех пяти провайдеров в целевом кластере и запустите проверку:

```bash
lab-cluster/bootstrap/verify.sh pre-pivot
```

Verify ждёт готовности провайдеров, узла, Cilium и Argo CD, показывает Applications, состояния Helm-релизов и список CAPI-ресурсов. Это диагностика для ручного просмотра: скрипт не проверяет каждое поле и не выполняет перенос.

Перед pivot убедитесь, что `lab-cluster-cluster` остаётся ручным и несинхронизированным, а список Cluster и PRM в целевом кластере пуст. В kind должны присутствовать один Cluster, ClusterClass, связанные Machines и шаблоны, все три PRM, SSH Secret, Secret со скриптами и оба HelmChartProxy с `InstallOnce`.

Изучите полный состав `clusterctl move --dry-run`: Cluster, ClusterClass, Machines, шаблоны, все необходимые Secrets (включая SSH-ключ и скрипты), три PRM, оба HelmChartProxy и связанные HelmReleaseProxy. У трёх PRM, обоих HelmChartProxy и Secrets с ключом и скриптами должна быть метка move. Если объект отсутствует в dry-run, исправьте метки или связи до переноса.

Сам перенос выполняется вручную:

```bash
clusterctl move \
  --kubeconfig lab-cluster/.state/kind.kubeconfig \
  --to-kubeconfig lab-cluster/.state/lab-cluster.kubeconfig \
  --namespace lab-cluster
lab-cluster/bootstrap/verify.sh post-pivot
```

После переноса проверьте, что перечисленные объекты находятся в целевом кластере, Helm-релизы имеют статус `deployed`, а Cilium и Argo CD работают. Сравните манифесты через diff `lab-cluster-cluster` в Argo CD. Затем вручную выполните Sync **без prune**, убедившись, что diff не меняет ожидаемую топологию. Application остаётся ручным и после pivot.

Kind удаляйте только после подтверждения работы целевого management-кластера:

```bash
kind delete cluster --name lab-cluster-bootstrap
```

Оба Helm-релиза Cilium и Argo CD установлены CAAPH с `reconcileStrategy: InstallOnce`. После установки CAAPH не обновляет их при изменении манифестов. После move у `HelmReleaseProxy` может не восстановиться `Ready`, поэтому `verify.sh` проверяет сами релизы и работающие компоненты. Обновление этих двух релизов потребует отдельного плана передачи управления.

## Статическая проверка

```bash
kubectl kustomize lab-cluster/capi/providers
kubectl kustomize lab-cluster/cluster
kubectl kustomize lab-cluster/cluster/addons
kubectl kustomize lab-cluster/cluster/remote-machines
for script in lab-cluster/bootstrap/*.sh lab-cluster/cluster/node-scripts/*.sh; do
  bash -n "$script"
done
git diff --check
```

Повторите сборку из копии репозитория без локального SSH Secret: GitOps не должен зависеть от этого файла. В общей сборке должны оставаться три PRM, два HelmChartProxy и сгенерированный Secret со скриптами; ссылки на него должны содержать одно и то же имя с хешем. Эти проверки подтверждают только статическую готовность; bootstrap и pivot проверяются отдельно на живом кластере.

Закреплены Kubernetes `v1.36.5`, CAPI `v1.14.3`, k0smotron `v2.3.0`, CAAPH `v0.6.4`, cert-manager chart `v1.21.2`, CAPI Operator chart `0.30.0`, Cilium chart `1.20.2` и Argo CD chart `10.9.6`. CAPI 1.14 поддерживает `contentFormat: Template` для подстановки версии Kubernetes в `setup-kube`. В CoreProvider и kubeadm ControlPlaneProvider включён `ClusterTopology`. Одна и та же сборка провайдеров применяется с обеих сторон pivot.
