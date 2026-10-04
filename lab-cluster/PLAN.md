# План bootstrap homelab-кластера на remote machines

## Цель

Создать `lab-cluster` на трёх физических машинах через временный kind management-кластер. До ручного pivot нужен один control-plane узел, Cilium, Argo CD, cert-manager и все CAPI-провайдеры в целевом кластере. Storage, ingress, VPN, прикладные сервисы и масштабирование control plane входят в следующие этапы.

| Параметр | Решение |
| --- | --- |
| Namespace Cluster, шаблонов, PRM и CAAPH-объектов | Совпадает с `Cluster.metadata.name`: `lab-cluster` |
| Kubernetes и API | `v1.36.5`, `10.60.80.1:6443` через kube-vip |
| Начальный control plane | 1 узел, выбранный из трёх PRM |
| CAPI core, kubeadm bootstrap/control plane | `v1.13.6` с обеих сторон pivot |
| k0smotron и CAAPH | `v2.1.1` и `v0.6.4` с обеих сторон pivot |
| Helm charts | cert-manager `v1.21.2`, CAPI Operator `0.29.0`, Cilium `1.20.2`, Argo CD `10.9.6` |
| Источник GitOps | `main` публичного HTTPS-репозитория |
| SSH-ключ | Локальный `ssh-key-secret.yaml`, исключённый из Git |

## Структура и владельцы

1. `capi/` содержит значения Helm для установки только CAPI Operator и отдельные манифесты пяти Provider CR. В kind оператор ставится Helm, а Provider CR применяются из `capi/providers/`. В целевом кластере те же версии ведут отдельные Argo CD Applications `capi-operator` и `capi-providers`.
2. `cluster/` содержит Cluster, ClusterClass, шаблоны, скрипты узлов, Cilium и Argo CD `HelmChartProxy`, а также отдельную сборку трёх PRM. Namespace для всех CAPI-объектов кластера равен имени Cluster. `bootstrap/cluster/` объединяет Cluster и PRM в **одну** операцию применения в kind. SSH Secret подаётся отдельно из локального файла; он не попадает в Git.
3. `applications/` содержит корневое Application и дочерние Applications. Argo CD chart создаёт корневое Application через `extraObjects`; оно автоматически синхронизирует определения дочерних Applications из Git. После запуска Argo CD скрипты ничего не применяют в целевой кластер.
4. `bootstrap/bootstrap.sh` создаёт kind, проверяет доступность машин и запускает начальную установку. `bootstrap/verify.sh pre-pivot|post-pivot` выполняет только чтение состояния и dry-run переноса. Сам `clusterctl move` всегда запускается вручную.

## Порядок выполнения

### 1. Подготовка

Проверить Docker, kind, kubectl, Helm, clusterctl, yq, OpenSSL и SSH; приватный Secret; свободный VIP `10.60.80.1`; SSH и `sudo -n` на `10.60.80.101–103`; общую L2-сеть для kube-vip. Скрипт проверяет TCP-доступ к машинам из pod в kind. Опубликовать манифесты в `main` до запуска GitOps.

### 2. Временный management-кластер

Создать kind с отдельным kubeconfig. Установить cert-manager chart и CAPI Operator chart через Helm. Отдельно применить пять Provider CR: core, kubeadm bootstrap, kubeadm control plane, k0smotron infrastructure и CAAPH addon; дождаться их `Ready`. Значения chart оператора не создают Provider CR, поэтому источник истины для них — `capi/providers/`.

### 3. Целевой кластер

Применить локальный SSH Secret с меткой `clusterctl.cluster.x-k8s.io/move: ""`, затем одной командой применить `bootstrap/cluster/`. Все три PRM, Secret со скриптами узлов и оба `HelmChartProxy` тоже имеют метку move. Дождаться API через VIP, установки Cilium, Ready узла. Затем применить Argo CD `HelmChartProxy`; оба proxy используют `InstallOnce`.

### 4. Самонастройка целевого кластера

CAAPH устанавливает Argo CD вместе с корневым Application. Корневое Application автоматически создаёт дочерние Applications. `cert-manager`, `capi-operator` и `capi-providers` синхронизируются автоматически; зависимости переживают временные ошибки через retry у оператора и провайдеров. `lab-cluster-capi` и `lab-cluster-remote-machines` создаются заранее **без** `syncPolicy.automated`, без auto-prune и без каскадного finalizer. До pivot их нельзя синхронизировать: Cluster и PRM в целевом management-кластере должны появиться только через move.

### 5. Ручной pivot

Запустить `bootstrap/verify.sh pre-pivot`. Он проверяет целевые компоненты, отсутствие дубликатов Cluster и PRM, `InstallOnce` у исходных proxy, метки move у трёх PRM и выполняет `clusterctl move --dry-run`. Проверить полный список объектов: Cluster, Machines, шаблоны, Secrets, три PRM, два `HelmChartProxy` и связанные `HelmReleaseProxy`. При отсутствии объекта исправить метки или связи.

Запустить `clusterctl move --kubeconfig ... --to-kubeconfig ... --namespace lab-cluster` вручную. Затем запустить `bootstrap/verify.sh post-pivot`, убедиться в сохранности Cluster, PRM, Secrets, proxy, Cilium, Argo CD и Helm-релизов. После успешного переноса вручную синхронизировать Application `lab-cluster-remote-machines`. Application `lab-cluster-capi` остаётся ручным. Kind удалить только после проверки.

## Инварианты и ограничения

- CAPI-манифесты Cluster не применяются автоматически в новый management-кластер, включая после pivot. Изменения в Git требуют ручного Sync `lab-cluster-capi`.
- PRM существуют в kind до pivot и переносятся с меткой move. Их Application создаётся автоматически, но синхронизируется только вручную по решению оператора. Удаление PRM из Git не вызывает автоматическое удаление объекта.
- `InstallOnce` сохраняет уже установленные Cilium и Argo CD при удалении исходных CAAPH-объектов во время move, но не обновляет релизы после успешной установки. У `HelmReleaseProxy` после move может не восстановиться `Ready`: проверять Helm-релизы и реальные компоненты. Изменение стратегии требует отдельного плана передачи управления релизами.
- Статические проверки `kubectl kustomize` и `bash -n` описаны в README. Работу сети, controller readiness и состав `clusterctl move --dry-run` можно подтвердить только на живом кластере.

## Источники

- [Cluster API move](https://cluster-api.sigs.k8s.io/clusterctl/commands/move)
- [k0smotron Remote Machine Provider](https://docs.k0smotron.io/v2.1.1/capi-remote/)
- [CAAPH](https://github.com/kubernetes-sigs/cluster-api-addon-provider-helm/blob/main/docs/quick-start.md)
