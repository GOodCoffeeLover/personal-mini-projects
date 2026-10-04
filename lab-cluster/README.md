# Homelab cluster bootstrap

`lab-cluster` создаётся на трёх физических машинах через временный kind management-кластер. Сначала работает один control-plane узел, выбранный k0smotron из пула. Адрес API через kube-vip — `10.60.80.1:6443`; Cilium заменяет kube-proxy. План и критерии готовности: [PLAN.md](PLAN.md).

## Каталоги

| Путь | Назначение |
| --- | --- |
| `capi/operator-values.yaml` | Значения Helm chart CAPI Operator без установки провайдеров |
| `capi/providers/` | Пять отдельных Provider CR и namespace их контроллеров |
| `cluster/` | Cluster, ClusterClass, шаблоны, скрипты узлов и Cilium `HelmChartProxy` |
| `cluster/addons/argo-cd/` | Argo CD `HelmChartProxy`, который создаёт корневое Application через `extraObjects` |
| `cluster/remote-machines/` | Три `PooledRemoteMachine` |
| `applications/` | Argo CD Applications для системных компонентов, CAPI и PRM |
| `bootstrap/cluster/` | Одноразовая сборка Cluster + PRM для применения в kind |
| `bootstrap/bootstrap.sh` | Создание kind и начальная установка кластера |
| `bootstrap/verify.sh` | Проверка перед pivot и после него; не меняет ресурсы |

Все CAPI-ресурсы конкретного кластера находятся в namespace `lab-cluster`, совпадающем с `Cluster.metadata.name`. Контроллеры и Argo CD работают в собственных namespace.

## Подготовка

Нужны Docker, kind, kubectl, Helm, clusterctl, yq, OpenSSL и SSH. Машины должны быть доступны по SSH с ноутбука и из pod в kind; у пользователя должен работать `sudo -n`. Для kube-vip узлы должны находиться в общей L2-сети, а `10.60.80.1` должен быть свободен. Установочные скрипты узлов скачивают компоненты Kubernetes и containerd.

Создайте локальный `lab-cluster/ssh-key-secret.yaml`, исключённый из Git: Kubernetes Secret с `metadata.name: ssh-key-personal-git` и `data.value`, содержащим base64 приватного SSH-ключа. Bootstrap проставит ему namespace `lab-cluster` и метку переноса. Ключ и kubeconfig не добавляйте в Git.

До запуска опубликуйте эту конфигурацию в `main` репозитория `https://github.com/GOodCoffeeLover/personal-mini-projects.git`: Argo CD читает именно опубликованные манифесты.

## Bootstrap

Из корня репозитория выполните:

```bash
lab-cluster/bootstrap/bootstrap.sh
```

Скрипт проверяет SSH и `sudo`, создаёт kind, проверяет сеть из pod, устанавливает cert-manager и CAPI Operator через Helm, применяет отдельные Provider CR и ждёт их готовности. Затем он применяет SSH Secret и **одну** Kustomize-сборку `bootstrap/cluster/` с Cluster и тремя PRM. Когда Cilium и узел готовы, он применяет Argo CD `HelmChartProxy`. Chart Argo CD создаёт корневое Application, которое автоматически создаёт остальные Applications из Git.

В целевом кластере cert-manager, CAPI Operator и Provider CR поддерживаются Argo CD автоматически. Applications `lab-cluster-capi` и `lab-cluster-remote-machines` создаются заранее, но у них нет автоматической синхронизации. Они не применяют манифесты до вашего ручного Sync; во время bootstrap синхронизировать их нельзя.

Kubeconfig хранятся по умолчанию в `lab-cluster/.state/kind.kubeconfig` и `lab-cluster/.state/lab-cluster.kubeconfig` (каталог исключён из Git). Пути можно изменить переменными `STATE_DIR`, `BOOTSTRAP_KUBECONFIG`, `TARGET_KUBECONFIG`, `SSH_SECRET`.

## Ручной pivot

Дождитесь автоматической установки всех пяти провайдеров в целевом кластере и запустите проверку:

```bash
lab-cluster/bootstrap/verify.sh pre-pivot
```

Изучите вывод `clusterctl move --dry-run`: в нём должны быть Cluster, связанные Machines и шаблоны, три PRM, SSH Secret, Secret со скриптами, оба `HelmChartProxy` и связанные `HelmReleaseProxy`. Если объект отсутствует, исправьте метки или связи до переноса.

Сам перенос выполняется вручную:

```bash
clusterctl move \
  --kubeconfig lab-cluster/.state/kind.kubeconfig \
  --to-kubeconfig lab-cluster/.state/lab-cluster.kubeconfig \
  --namespace lab-cluster
lab-cluster/bootstrap/verify.sh post-pivot
```

После проверки перенесённых объектов вручную синхронизируйте `lab-cluster-remote-machines` в Argo CD. `lab-cluster-capi` тоже остаётся ручным и без каскадного finalizer; его синхронизация не нужна для pivot. У обоих Applications выключен prune. Kind удаляйте только после проверки работы целевого кластера.

Оба Helm-релиза Cilium и Argo CD установлены CAAPH с `reconcileStrategy: InstallOnce`. После установки CAAPH не обновляет их при изменении манифестов. После move у `HelmReleaseProxy` может не восстановиться `Ready`, поэтому `verify.sh` проверяет сами релизы и работающие компоненты. Обновление этих двух релизов потребует отдельного плана передачи управления.

## Статическая проверка

```bash
kubectl kustomize lab-cluster/capi/providers
kubectl kustomize lab-cluster/cluster
kubectl kustomize lab-cluster/cluster/addons/argo-cd
kubectl kustomize lab-cluster/cluster/remote-machines
kubectl kustomize lab-cluster/bootstrap/cluster
bash -n lab-cluster/bootstrap/*.sh lab-cluster/cluster/node-scripts/*.sh
```

Закреплены Kubernetes `v1.36.5`, CAPI `v1.13.6`, k0smotron `v2.1.1`, CAAPH `v0.6.4`, cert-manager chart `v1.21.2`, CAPI Operator chart `0.29.0`, Cilium chart `1.20.2` и Argo CD chart `10.9.6`.
