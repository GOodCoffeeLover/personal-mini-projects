# Homelab cluster bootstrap

`lab-cluster` создаётся на трёх физических машинах через временный kind management-кластер. Все три машины используются как control-plane с локальным etcd. Адрес API через kube-vip — `10.60.80.1:6443`; Cilium заменяет kube-proxy. План и критерии готовности: [PLAN.md](PLAN.md).

## Каталоги

| Путь | Назначение |
| --- | --- |
| `capi/operator-values.yaml` | Значения Helm chart CAPI Operator без установки провайдеров |
| `capi/providers/` | Пять отдельных Provider CR и namespace их контроллеров |
| `cluster/` | Общая сборка Cluster, ClusterClass, шаблонов, скриптов, трёх PRM и обоих аддонов |
| `cluster/addons/` | Cilium и Argo CD `HelmChartProxy`; образ Redis закреплён в values аддона Argo CD |
| `cluster/remote-machines/` | Три `PooledRemoteMachine`; локальный SSH Secret не входит в сборку |
| `applications/` | Корневое Application, системные Applications; ручные `lab-cluster-cluster`, `cilium` и `argocd` |
| `bootstrap/bootstrap.sh` | Создание kind и начальная установка кластера |
| `bootstrap/verify.sh` | Проверка перед pivot и после него; не меняет ресурсы |

Все CAPI-ресурсы конкретного кластера находятся в namespace `lab-cluster`, совпадающем с `Cluster.metadata.name`. Контроллеры и Argo CD работают в собственных namespace.

## Подготовка

Нужны Docker, kind `v0.33.0`, kubectl `v1.37.1`, Helm 3, clusterctl `v1.14.3`, Mike Farah yq 4, OpenSSL и SSH. Образ kind должен использовать Kubernetes `1.33–1.37`, поддерживаемый CAPI 1.14. Узлы — Debian/Ubuntu с systemd, `curl`, SSH и доступным `sudo -n`. Скрипты скачивают закреплённые версии компонентов, пропуская уже установленные версии и файлы.

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

Скрипт создаёт или использует существующий kind, устанавливает cert-manager и CAPI Operator через Helm, применяет пять Provider CR и ждёт их готовности. Затем отдельно применяет SSH Secret и **одну** Kustomize-сборку `cluster/` с Cluster, тремя PRM и обоими HelmChartProxy. CAAPH устанавливает Cilium и Argo CD; скрипт ждёт Cilium, Ready узла и готовности HelmChartProxy Argo CD. После этого он ждёт `Established` у CRD `applications.argoproj.io` и готовности `argocd-server`, затем применяет `applications/root.yaml` в целевой кластер. Корневое Application автоматически создаёт остальные Applications из Git.

Корневое Application применяется отдельно от Helm-релиза: CRD chart Argo CD находятся среди обычных шаблонов, поэтому на первой установке Helm ещё не может проверить API для Application из `extraObjects`. Если ожидание HelmChartProxy, CRD или сервера заканчивается ошибкой, bootstrap останавливается до применения корневого Application.

Для Redis задан официальный образ `docker.io/library/redis:8.6.4-alpine`. Версия сохраняется, источник меняется с ECR Public на Docker Hub из-за наблюдавшихся таймаутов скачивания с узла.

В целевом кластере cert-manager, CAPI Operator и Provider CR поддерживаются Argo CD автоматически. `lab-cluster-cluster` охватывает Cluster, пул машин и аддоны, но синхронизируется только вручную, без автоматического prune и каскадного finalizer. **До pivot не синхронизируйте это Application:** Cluster и PRM должны попасть в целевой management-кластер через move.

Корневое Application также создаёт отдельные `cilium` и `argocd`, но они остаются без автоматического Sync и каскадного finalizer, с `Prune=false`. После передачи управления они применяют ресурсы chart напрямую; `lab-cluster-cluster` продолжает содержать только bootstrap proxy этих аддонов. Первую синхронизацию проводите после pivot по инструкции ниже, отдельно от масштабирования control-plane.

Kubeconfig хранятся по умолчанию в `lab-cluster/.state/kind.kubeconfig` и `lab-cluster/.state/lab-cluster.kubeconfig` (каталог исключён из Git). Пути можно изменить переменными `STATE_DIR`, `BOOTSTRAP_KUBECONFIG`, `TARGET_KUBECONFIG`, `SSH_SECRET`; имя kind — переменной `KIND_NAME`. При последующих вызовах verify и move используйте те же значения. Bootstrap предназначен для этапа до pivot; существующий управляемый кластер повторно создавать не нужно.

## kube-vip при замене control-plane

`setup-kube-vip` вызывается из CAPI без аргументов. Для первой ноды он читает `controlPlaneEndpoint` из `/run/kubeadm/kubeadm.yaml` и использует `/etc/kubernetes/super-admin.conf`. При join читает `apiServerEndpoint` из `/run/kubeadm/kubeadm-join-config.yaml` и использует `/etc/kubernetes/admin.conf`. Если на повторно используемой машине остались оба конфига, выбирается более новый. Отсутствующий конфиг или некорректный endpoint завершают скрипт ошибкой.

Для восстановления VIP на уже работающем control-plane доступны два аргумента: endpoint и абсолютный путь к kubeconfig. После установки актуального скрипта на ноду выполните на ней:

```bash
sudo bash /usr/local/sbin/setup-kube-vip \
  10.60.80.1:6443 /etc/kubernetes/admin.conf
```

В этом режиме временный kubeadm-конфиг не нужен. Скрипт записывает static Pod в `/etc/kubernetes/manifests/kube-vip.yaml`, только если содержимое изменилось. Kubelet подхватит его без перезапуска служб. kube-vip использует локальный API через loopback alias, поэтому запуск не зависит от доступности самого VIP.

Проверьте появление VIP на интерфейсе ноды (`ip -br -4 address`), готовность pod kube-vip и ответ API через VIP. Если используется HTTP-прокси, исключите приватные адреса из проксирования:

```bash
export NO_PROXY="${NO_PROXY},10.60.80.1,10.60.80.102"
kubectl --kubeconfig lab-cluster/.state/lab-cluster.kubeconfig get --raw=/readyz
kubectl --kubeconfig lab-cluster/.state/lab-cluster.kubeconfig get nodes -o wide
```

После обновления до `v1.37.1` VIP восстановлен на `asus-pn41-2` (`10.60.80.102`) этим способом. Исправленный скрипт установлен на ноду напрямую. Изменение скрипта в репозитории меняет хеш Secret и может вызвать следующий rollout при ручной синхронизации `lab-cluster-cluster`; доставку обновлённых шаблонов проводите отдельно в плановое окно.

## Переход существующего control-plane с одной реплики на три

В репозитории заданы три реплики и `spec.rollout.strategy.rollingUpdate.maxSurge: 0` в шаблоне KCP. При обновлении CAPI сначала удаляет старую Machine, затем создаёт замену на освободившемся хосте. Все три PRM заняты, дополнительная машина для rollout не требуется. Два оставшихся участника etcd сохраняют кворум, если исправны. `maxSurge: 0` требует минимум три реплики KCP.

Существующий кластер с одной репликой переводите поэтапно:

1. Разберите оставшийся Node `asus-pn41-1`: после подтверждения удаления старой Machine и очистки хоста удалите устаревшую запись Node перед повторным использованием машины.
2. Примените только Cluster с тремя репликами, сохранив текущие шаблоны и `maxSurge: 1`. Из корня репозитория:

   ```bash
   kubectl --kubeconfig lab-cluster/.state/lab-cluster.kubeconfig \
     --namespace lab-cluster apply -f lab-cluster/cluster/cluster.yaml
   ```

3. Дождитесь трёх готовых Machines и Nodes, трёх готовых реплик KCP и исправности всех трёх участников etcd. До этого полную сборку `cluster/` не применяйте.
4. Вручную синхронизируйте `lab-cluster-cluster` без prune. Этот шаг доставит `maxSurge: 0` и исправленный скрипт kube-vip; изменение Secret со скриптами может вызвать замену Machines. Дождитесь завершения rollout и повторно проверьте три реплики, etcd, VIP и Cilium.

Для нового кластера bootstrap сразу использует три реплики и конечную стратегию; промежуточный этап требуется для существующего кластера с одной репликой.

## Ручной pivot

Дождитесь автоматической установки всех пяти провайдеров в целевом кластере и запустите проверку:

```bash
lab-cluster/bootstrap/verify.sh pre-pivot
```

Verify ждёт готовности провайдеров, узлов, Cilium, сервера, application-controller, repo-server и Redis Argo CD, показывает Applications, состояния bootstrap Helm-релизов и список CAPI-ресурсов. После передачи управления Argo CD вывод Helm не отражает новые Sync; актуальны Applications и работающие ресурсы. Ручные `cilium` и `argocd` могут быть `OutOfSync` до первого Sync. Это диагностика для ручного просмотра: скрипт не проверяет каждое поле и не выполняет перенос.

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

Оба Helm-релиза Cilium и Argo CD установлены CAAPH с `reconcileStrategy: InstallOnce`. После установки CAAPH не обновляет их при изменении манифестов. После move у `HelmReleaseProxy` может не восстановиться `Ready`, поэтому готовность оценивается по работающим компонентам. Следующий этап — передача дальнейшего управления отдельным Applications.

Правки образа Redis и порядка создания корневого Application проверены локально. Изменение файлов или HelmChartProxy само по себе не обновляет успешно установленный релиз с `InstallOnce`. Если текущий Redis ещё использует старый источник образа, его исправление будет отдельным изменением при передаче управления, а не пустым Sync. Успешное скачивание из Docker Hub на физическом узле требует отдельной проверки.

## Передача Cilium и Argo CD под управление Applications

| Application | Chart | Версия | Release name | Namespace |
| --- | --- | --- | --- | --- |
| `cilium` | `cilium` | `1.20.2` | `cilium` | `kube-system` |
| `argocd` | `argo-cd` | `10.9.6` | `argocd` | `argocd` |

Оба приложения используют ручной Sync и server-side apply. Root Application создаёт и поддерживает их определения, но не запускает синхронизацию дочерних ресурсов. Начальные `valuesObject` совпадают с `valuesTemplate` соответствующих HelmChartProxy. При изменении версии или конфигурации меняйте оба определения: в `applications/` — для дальнейшего управления, в `cluster/addons/` — для нового bootstrap. Общий генератор values не используется.

Cilium chart `1.20.2` по умолчанию включает Hubble TLS. Helm при установке может использовать `lookup` для существующих сертификатов, а рендеринг Argo CD генерирует новые. Поэтому Application игнорирует **только пять полей сертификатов** в Secrets `kube-system/cilium-ca` и `kube-system/hubble-server-certs`; `RespectIgnoreDifferences=true` сохраняет их и при Sync. Для отсутствующего Secret это правило не действует. Ротация CA/сертификатов требует отдельной операции; при включении Hubble relay или других TLS-компонентов пересмотрите правила для новых Secrets. Широких исключений для Secrets, CRD или workloads нет. [RespectIgnoreDifferences](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/#respect-ignore-differences-configs).

Chart Argo CD `10.9.6` уже исключает внутренние `CiliumIdentity`, `CiliumEndpoint` и `CiliumEndpointSlice` из отслеживания. Сохраните эти настройки в `argocd-cm` при последующих изменениях. Redis init hook запускается как PreSync: он проверяет существующий Secret `argocd-redis` и создаёт его при отсутствии; передача управления должна сохранить существующий пароль. `argocd-secret` содержит создаваемые во время работы данные, которые также нужно сохранить.

Порядок первой передачи управления:

1. Завершите переход control-plane на три реплики и его rollout. Подтвердите готовность трёх Nodes, etcd, VIP, Cilium и Argo CD. На новом кластере сначала завершите bootstrap и pivot.
2. Опубликуйте определения в `main`, дождитесь появления `cilium` и `argocd` от root Application. Оба приложения должны оставаться без автоматического Sync; до ручной синхронизации ресурсы аддонов не меняются.
3. Сравните установленные chart/values (`helm list`, `helm get values`) с таблицей и Applications. Проверьте у **обоих** HelmReleaseProxy `spec.reconcileStrategy: InstallOnce` и аннотацию `helmreleaseproxy.addons.cluster.x-k8s.io/release-successfully-installed: "true"`. Если отметки нет, разберите состояние CAAPH до Sync; не подставляйте её без подтверждения успешной установки. Существующие proxy сохраняются: пересоздание HelmReleaseProxy теряет отметку и может снова вызвать Helm install/upgrade.
4. Сохраните манифесты и Secrets для восстановления извне по инструкции ниже. Проверьте diff каждого Application: имена, namespace, selectors, образы, CRD, Secrets и hooks. Различия tracking metadata и ожидаемый запуск Redis hook допустимы; остальные различия сначала разберите. При несовпадении установленных версий остановите передачу и согласуйте конфигурацию, не совмещая её с обновлением версий.
5. Вручную выполните Sync только `argocd` без prune, Force и Replace. Дождитесь завершения Redis hook, готовности application-controller, repo-server, Redis и сервера. Убедитесь в сохранности доступа, существующих Applications и Secrets, а также исключений внутренних ресурсов Cilium.
6. Затем вручную выполните Sync только `cilium` без prune, Force и Replace. Подтвердите готовность агентов на всех трёх узлах, operator и Envoy, работу DNS, связь pod между узлами и доступ к API через VIP.
7. Выполните `lab-cluster/bootstrap/verify.sh post-pivot` и просмотрите `Synced`/`Healthy` обоих Applications. Повторный refresh не должен создавать diff сертификатов. После Sync `helm history` и `helm status` остаются сведениями об установке CAAPH и больше не показывают актуальное состояние аддонов.

`Prune=false` и отсутствие finalizer защищают от удаления при обычном Sync/удалении Application, но не предотвращают ошибочные изменения ресурсов при ручном Sync. Ошибка Cilium может остановить связь Argo и CAPI с API; ошибка обновления Argo — сам механизм доставки. Обновляйте компоненты по одному и сохраняйте внешний kubeconfig и SSH-доступ. CAAPH также работает в целевом кластере и после `InstallOnce` не восстанавливает аддоны автоматически.

### Восстановление без работающего Argo CD

Перед **первым** Sync сохраните исходную установку и фактические Secrets локально, из корня репозитория:

```bash
umask 077
mkdir -p lab-cluster/.state/addon-recovery
helm --kubeconfig lab-cluster/.state/lab-cluster.kubeconfig -n argocd get manifest argocd \
  > lab-cluster/.state/addon-recovery/argocd.yaml
helm --kubeconfig lab-cluster/.state/lab-cluster.kubeconfig -n kube-system get manifest cilium \
  > lab-cluster/.state/addon-recovery/cilium.yaml
kubectl --kubeconfig lab-cluster/.state/lab-cluster.kubeconfig -n argocd \
  get secret argocd-secret argocd-redis -o yaml > lab-cluster/.state/addon-recovery/argocd-secrets.yaml
kubectl --kubeconfig lab-cluster/.state/lab-cluster.kubeconfig -n kube-system \
  get secret cilium-ca hubble-server-certs -o yaml > lab-cluster/.state/addon-recovery/cilium-secrets.yaml
```

Эти файлы содержат секреты и остаются в исключённом из Git `.state/`. Перед последующими обновлениями готовьте манифесты из последней проверенной ревизии Git через `helm template` с её версией chart и values. Старые `helm get manifest` после передачи управления уже не отражают последнюю исправную конфигурацию.

Если Argo не работает, восстановите сначала сеть при необходимости, затем Argo через внешний kubeconfig. Для выбранного компонента, например Cilium, исключите Secrets из общего применения, чтобы не заменить действующие сертификаты результатом рендеринга:

```bash
yq 'select(.kind != "Secret")' lab-cluster/.state/addon-recovery/cilium.yaml \
  | kubectl --kubeconfig lab-cluster/.state/lab-cluster.kubeconfig apply \
      -n kube-system --server-side --force-conflicts --field-manager=argocd-controller -f -
```

Для Argo используйте `argocd.yaml` и namespace `argocd` (`-n argocd` у kubectl); для Cilium namespace — `kube-system` (`-n kube-system`). `--force-conflicts` здесь используется только для аварийного возврата сохранённой конфигурации, без удаления и пересоздания ресурсов. Потерянные Secrets восстановите из отдельных резервных копий, сохранив текущие, если они исправны. Верните исправную конфигурацию в Git перед следующим ручным Sync. `helm uninstall` удалит работающие ресурсы; `helm rollback` использует устаревшую историю и не является способом отката изменений Argo. Если VIP недоступен, используйте доступный API физического control-plane через отдельный kubeconfig; при необходимости восстановите kube-vip через SSH по инструкции выше.

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

Закреплены Kubernetes `v1.37.1`, CAPI `v1.14.3`, k0smotron `v2.3.0`, CAAPH `v0.6.4`, cert-manager chart `v1.21.2`, CAPI Operator chart `0.30.0`, Cilium chart `1.20.2` и Argo CD chart `10.9.6`. CAPI 1.14 поддерживает `contentFormat: Template` для подстановки версии Kubernetes в `setup-kube`. В CoreProvider и kubeadm ControlPlaneProvider включён `ClusterTopology`. Одна и та же сборка провайдеров применяется с обеих сторон pivot.
