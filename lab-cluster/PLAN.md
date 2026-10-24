# План bootstrap homelab-кластера на remote machines

## Цель и состояние

Кластер перенесён из kind в целевой management-кластер и обновлён до `v1.37.1`. Заданы три реплики control-plane и `maxSurge: 0`; при последней диагностике `.102` уже прошла повторный join, а CAPI начал замену `.101`. Обнаружен оставшийся на `.102` VIP после reset и неисправный kube-vip на `.101` с прежним kubeconfig. В файлах подготовлены очистка VIP и временного состояния, переход kube-vip на `admin.conf` после bootstrap и отдельные ручные Applications Cilium и Argo CD. Сначала нужно подтвердить готовность трёх control-plane, затем доставить исправления скриптов и завершить rollout. После этого управление аддонами передаётся Applications отдельными ручными Sync. Команды запуска и ручной диагностики находятся в [README.md](README.md).

Целевая архитектура: временный kind management-кластер создаёт три control-plane узла на трёх физических машинах. На них работают Cilium, Argo CD, cert-manager и пять CAPI-провайдеров. Затем управление переносится в целевой кластер вручную. Существующий кластер масштабируется с одной до трёх реплик после pivot. Storage, ingress, VPN и прикладные сервисы входят в следующие этапы.

| Параметр | Решение |
| --- | --- |
| Namespace Cluster, шаблонов, PRM и CAAPH-объектов | Совпадает с `Cluster.metadata.name`: `lab-cluster` |
| Kubernetes и API | `v1.37.1`, `10.60.80.1:6443` через kube-vip |
| Control plane | 3 узла с локальным etcd; используются все три PRM |
| Rollout KCP | `RollingUpdate`, `maxSurge: 0`: сначала удаление, затем замена Machine |
| CAPI core, kubeadm bootstrap/control plane | `v1.14.3` с обеих сторон pivot |
| k0smotron и CAAPH | `v2.3.0` и `v0.6.4` с обеих сторон pivot |
| Helm charts | cert-manager `v1.21.2`, CAPI Operator `0.30.0`, Cilium `1.20.2`, Argo CD `10.9.6` |
| Redis для Argo CD | `docker.io/library/redis:8.6.4-alpine` |
| Управление Cilium и Argo CD | CAAPH `InstallOnce` для bootstrap; отдельные ручные Applications для дальнейших изменений |
| Источник GitOps | `main` публичного HTTPS-репозитория |
| SSH-ключ | Локальный `cluster/remote-machines/ssh-key-secret.yaml`, исключённый из Git |

## Подготовка репозитория

- [x] Закрепить новые версии провайдеров и оператора одинаково для kind и целевого кластера.
- [x] Включить `spec.manager.featureGates.ClusterTopology` у CoreProvider и kubeadm ControlPlaneProvider. CAPI 1.14 нужен также для `contentFormat: Template`, подставляющего версию Kubernetes в установочный скрипт.
- [x] Объединить Cluster, ClusterClass, шаблоны, скрипты узлов, три PRM и оба HelmChartProxy в сборку `cluster/`.
- [x] Исключить приватный SSH Secret из Kustomize; bootstrap применяет его отдельно с namespace и меткой переноса.
- [x] Оставить одно ручное Application `lab-cluster-cluster`, без автоматического prune и каскадного finalizer.
- [x] Исправить bootstrap и сократить verify до команд готовности, вывода состояния и dry-run переноса.
- [x] Сохранить пропуск уже установленных версий и файлов в скриптах узлов. SSH, sudo и сетевые preflight-проверки выполнять вручную по README.
- [x] Переключить Redis Argo CD с ECR Public на Docker Hub с сохранением версии `8.6.4-alpine`.
- [x] Убрать корневое Application из Helm `extraObjects`; bootstrap применяет существующий `applications/root.yaml` после готовности HelmChartProxy Argo CD, CRD Application и сервера.
- [x] Подтвердить статическую готовность: Kustomize без локального SSH Secret, ссылки на Secret со скриптами, namespace, метки move, структура по CRD провайдеров, `bash -n` и `git diff --check`.

Статические проверки выполнены: четыре сборки проходят из копии без приватного Secret; структура ресурсов проверена по опубликованным CRD закреплённых версий. Helm chart оператора рендерится с общими для bootstrap и Argo CD values и не создаёт Provider CR. Прогоны с подставными CLI подтвердили порядок bootstrap, остановку при ошибке Cilium и отсутствие изменений ресурсов и локального состояния в verify. CEL-валидация, webhooks и работа контроллеров требуют живого API-сервера и ещё не проверены.

Исправления Argo CD также проверены локально: chart `10.9.6` рендерит Redis из Docker Hub и три CRD без корневого Application в релизе. Прогоны с подставными CLI подтверждают отдельное применение корневого Application в целевой кластер после трёх успешных ожиданий; ошибка ожидания предотвращает apply, а ошибка самого apply завершает скрипт без сообщения об успехе. Эти правки в живой кластер ещё не применены.

## Владельцы ресурсов и порядок bootstrap

1. В kind Helm устанавливает cert-manager и CAPI Operator. Затем применяются пять Provider CR из `capi/providers/`; скрипт ждёт их готовности. Значения Helm chart оператора сами провайдеров не создают.
2. Bootstrap применяет локальный SSH Secret отдельно, затем одной командой всю сборку `cluster/`. CAAPH устанавливает Cilium и Argo CD, когда целевой API становится доступен. Скрипт ждёт Cilium, Ready узла и Argo CD.
3. После готовности HelmChartProxy Argo CD bootstrap ждёт `Established` у CRD `applications.argoproj.io` и готовности `argocd-server`, затем отдельно применяет `applications/root.yaml` в целевой кластер. При ошибке ожидания Application не применяется. Такой порядок устраняет проверку Application до установки его CRD в первом Helm-релизе. Корневое Application автоматически создаёт дочерние Applications из Git. cert-manager, оператор и Provider CR в целевом кластере поддерживаются автоматически; версии и feature gates берутся из тех же конфигураций, что и в kind.
4. `lab-cluster-cluster` создаётся заранее, но до pivot его нельзя синхронизировать. Cluster и PRM должны появиться в целевом management-кластере только через move. После переноса приложение также остаётся ручным. Корневое Application создаёт также ручные `cilium` и `argocd`; первая передача управления ими выполняется после pivot и отдельно от rollout control-plane.
5. `verify.sh pre-pivot|post-pivot` выполняет чтение состояния и, перед pivot, dry-run. Скрипт не меняет ресурсы и не выполняет сам перенос. Вывод нужно просмотреть: завершение команд не заменяет проверку состава объектов.

## Завершение запуска на физических машинах

- [ ] Проверить инструменты, Debian/Ubuntu с systemd на узлах, SSH и `sudo -n`, доступ из kind, свободный VIP и общую L2-сеть. Опубликовать подготовленные манифесты в `main`.
- [ ] Выполнить bootstrap; подтвердить доступность API, Cilium, узла и Argo CD.
- [ ] Применить исправления Redis и порядка запуска GitOps к существующему кластеру; подтвердить скачивание образа с физического узла, готовность Helm-релиза Argo CD и корневого Application.
- [ ] Дождаться готовности пяти провайдеров в целевом кластере, сохраняя `lab-cluster-cluster` несинхронизированным.
- [ ] Выполнить pre-pivot проверку. В целевом кластере не должно быть Cluster и PRM. В dry-run должны присутствовать Cluster, ClusterClass, Machines, шаблоны, необходимые Secrets, три PRM и оба HelmChartProxy с HelmReleaseProxy.
- [ ] Вручную выполнить `clusterctl move` и post-pivot проверку. Убедиться в сохранности перенесённых объектов и работе компонентов.
- [ ] Просмотреть diff и вручную синхронизировать `lab-cluster-cluster` без prune, убедившись, что синхронизация не меняет ожидаемую топологию.
- [ ] Удалить kind только после подтверждения работы целевого management-кластера.

## Восстановление VIP после обновления до v1.37.1

Rollout заменил control-plane на `.102`, но прежний скрипт kube-vip читал только init-конфиг kubeadm и всегда выбирал `super-admin.conf`. На join-ноде нужен другой конфиг и `admin.conf`; static Pod не был создан. После удаления старого control-plane VIP исчез, хотя API новой ноды оставался исправен.

- [x] Исправить автоматический выбор init/join-конфига и kubeconfig; при наличии двух файлов выбирать более новый.
- [x] Добавить ручной запуск с endpoint и kubeconfig без зависимости от временных файлов kubeadm; ошибки входных данных не маскируются успешным пропуском.
- [x] Проверить init, join, повторное использование хоста, ручной запуск, ошибочные входные данные и отсутствие перезаписи неизменного манифеста.
- [x] Установить исправленный скрипт и static Pod на `.102`; подтвердить kube-vip Running, VIP на интерфейсе, `/readyz=ok` через VIP и владение lease нодой `asus-pn41-2`.
- [x] Подтвердить готовность текущей Machine на `v1.37.1`, Cilium operator, Argo CD и системных pod на `.102`.
- [ ] Доставить обновлённый Secret со скриптами и CAPI-шаблоны в отдельное плановое окно: ручной Sync может вызвать следующий rollout. При восстановлении они не применялись.
- [ ] Разобрать оставшийся Node `asus-pn41-1` в `NotReady,SchedulingDisabled`. Из-за него готовность DaemonSet Cilium и Envoy остаётся `1/2`; старые pod в API могут показывать устаревший статус. Его удаление не выполнялось при восстановлении VIP.

## Три реплики control-plane и rollout без дополнительной машины

- [x] Задать `Cluster.spec.topology.controlPlane.replicas: 3`.
- [x] Задать `KubeadmControlPlaneTemplate.spec.template.spec.rollout.strategy` с `type: RollingUpdate` и `rollingUpdate.maxSurge: 0`.
- [x] Описать поэтапную доставку в README; живой кластер при изменении файлов не менялся.
- [ ] Очистить устаревшую запись Node перед повторным использованием хоста.
- [ ] Применить только Cluster с тремя репликами при текущих шаблонах и `maxSurge: 1`; дождаться трёх готовых реплик и исправности etcd.
- [ ] Ручным Sync без prune доставить `maxSurge: 0` и исправленный скрипт kube-vip; дождаться завершения возможного rollout, проверить три реплики, etcd, VIP и Cilium.

CAPI запрещает `maxSurge: 0` при числе реплик меньше трёх. При трёх исправных участниках etcd удаление одного на время замены сохраняет кворум из двух. Все три PRM используются, поэтому стратегия освобождает хост перед созданием новой Machine.

## Очистка VIP и повторное использование control-plane

Повторный join `.102` обращался к VIP `10.60.80.1:6443`, который остался локальным адресом машины после удаления прежнего control-plane. Собственного API на ней ещё не было, хотя прямые обращения к `.101` и `.103` проходили. Cleanup также сохранял старые файлы `/run/kubeadm` и отметку успешного bootstrap в `/run/cluster-api`.

- [x] Сохранить интерфейс cleanup без аргументов; до reset удалять манифест kube-vip при работающем kubelet и ждать завершения контейнера через `crictl` без явного endpoint.
- [x] Опрос выполнять раз в секунду до 90 секунд; при ошибке CRI или превышении времени не запускать reset. Для static Pod задать `terminationGracePeriodSeconds: 60`.
- [x] После успешного reset очищать временные каталоги kubeadm и Cluster API вместе с прежним состоянием CNI; ошибки reset не маскировать.
- [x] При автоматическом выборе kubeconfig отдавать приоритет существующему непустому файлу `admin.conf`; для первоначального init сохранить `super-admin.conf`.
- [x] Добавить `hostPath.type: File` и повторный запуск setup из `postKubeadmCommands`, переводящий kube-vip на `admin.conf` после успешного kubeadm.
- [x] Описать доставку с паузой CAPI и предварительной установкой скриптов на все существующие машины в README.
- [ ] Подтвердить завершение текущих замен и готовность трёх control-plane, приостановить CAPI и установить новые cleanup/setup на все три узла через SSH.
- [ ] Исправить старые манифесты kube-vip ручным запуском с `admin.conf`, подтвердить готовность трёх Pod и одного владельца lease.
- [ ] Доставить новый Secret и KCP-шаблон, возобновить CAPI и подтвердить завершение последовательного rollout, исправность etcd, VIP и повторного join.

Выбрана штатная остановка kube-vip перед reset, без ручного удаления IP в cleanup или setup. Если kube-vip уже аварийно завершился и оставил адрес, остаток устраняется вручную. Обновлённый Secret сам по себе не заменяет cleanup на уже работающих узлах, поэтому установка локальных файлов и обновление манифестов kube-vip предшествуют применению нового шаблона. Изменения этого этапа в кластер не применялись.

Локальные прогоны с подставными командами подтвердили порядок удаления манифеста, ожидания контейнера и reset; завершение после нескольких опросов; отсутствие контейнера и повторный запуск; остановку до reset при ошибке CRI или таймауте; сохранение временного состояния при ошибке reset. Вызов `crictl` проверен без явного endpoint, команды изменения IP отсутствуют. Проверены grace period, init → postKubeadm → повторный запуск и ручной режим setup. Все четыре Kustomize-сборки проходят без SSH Secret; содержимое и ссылки сгенерированного Secret согласованы, KCP соответствует закреплённой CRD. Скрипты и 16 bash-примеров README проходят `bash -n`, diff — `git diff --check`. Реальный cleanup и rollout этим прогоном не выполнялись.

## Передача Cilium и Argo CD отдельным Applications

- [x] Добавить ручные Applications `cilium` и `argocd` с прежними версиями, releaseName, namespace и values, без автоматического Sync, prune и каскадного finalizer; включить server-side apply.
- [x] Сохранить CAAPH `InstallOnce` для bootstrap; оставить inline values и указать парное обновление конфигурации Applications и bootstrap proxy.
- [x] Сохранить существующие сертификаты Cilium через точечные `ignoreDifferences` для пяти полей двух TLS Secrets и `RespectIgnoreDifferences=true`.
- [x] Обновить read-only verify: показывать оба Applications, проверять основные компоненты Argo CD и объяснять ограничение bootstrap Helm status.
- [x] Описать в README порядок передачи управления и восстановление без работающего Argo CD.
- [ ] После завершения работ с control-plane подтвердить установленные версии/values и успешную установку обоих HelmReleaseProxy в режиме `InstallOnce`; сохранить proxy и аннотации успешной установки.
- [ ] Создать Applications без Sync, сохранить резервные манифесты и Secrets вне Git, разобрать live diff, CRD и hooks.
- [ ] Вручную синхронизировать сначала Cilium с пулом и L2-политикой, затем Argo CD с LoadBalancer; проверить готовность компонентов, сохранность Secrets, DNS, межузловую связь, API и отсутствие повторяющегося diff сертификатов.

CAAPH после успешной установки не меняет релизы, но пересоздание HelmReleaseProxy может повторно запустить install/upgrade. Argo CD использует Helm только для рендеринга: после передачи управления Helm history не обновляется. Ошибка Cilium может остановить сеть и доставку исправления, ошибка Argo — его собственные контроллеры; оба компонента обновляются вручную и по одному, восстановление выполняется внешним kubeconfig и при необходимости SSH. Версии chart и число Cilium operator replicas на этом этапе не меняются. Применение в живой кластер выполняется отдельно.

Локально подтверждены совпадение версий, releaseName, namespace и values с CAAPH и структура всех Applications по CRD chart Argo CD `10.9.6`. Рендеры bootstrap и Applications совпадают, кроме пяти полей сертификатов Cilium, покрытых точечными исключениями; Argo сохраняет исключения внутренних Cilium CR, три CRD и Redis hook с правами только get/create Secrets. Все четыре Kustomize-сборки проходят без приватного SSH Secret. Прогоны verify с подставными CLI для pre/post-pivot подтверждают отсутствие мутаций и создания локального состояния, вывод обоих Applications и остановку при ошибке готовности application-controller. Live diff, первый Sync и сохранность действующих Secrets ещё требуют проверки в кластере.

## LoadBalancer через два источника Application Cilium

- [x] Перевести существующий `cilium` на `spec.sources`: закреплённый Helm chart и Kustomize-сборка `cilium-config/` из `main`, сохранив ручной Sync и правила сертификатов.
- [x] Включить L2 announcements и API rate limit `10/20` одинаково в Application и bootstrap values Cilium.
- [x] Добавить общий пул `10.60.80.2–10.60.80.11` и L2-политику для LB IP на физических интерфейсах `en*`/`eth*`, с sync wave `1`.
- [x] Настроить Application Argo CD на LoadBalancer `10.60.80.2`, класс `io.cilium/l2-announcer` и `externalTrafficPolicy: Cluster`. Bootstrap сохраняет `ClusterIP`, чтобы Helm завершился до создания пула.
- [x] Описать диапазон, различие bootstrap/Applications и последовательность Cilium → Argo CD в `cilium-config/README.md` и основной инструкции.
- [ ] Перед доставкой подтвердить свободу адресов и исключение диапазона из DHCP; проверить, что другой LB-сервис не занял `.2`.
- [ ] После завершения работ с control-plane вручную синхронизировать Cilium, проверить агентов и пул без конфликта, затем Argo CD; подтвердить выдачу `.2`, L2 lease и HTTPS-доступ из LAN.

Локально проверены рендеры обоих закреплённых chart, объединение источников без дублирующихся ресурсов, структура Applications по CRD Argo CD, L2-настройки и RBAC leases, десять адресов пула без API VIP и IP узлов, пять Kustomize-сборок. В рендере Argo CD от bootstrap отличается только Service сервера; Redis и TLS сохраняются. Новые CR требуют проверки API-сервером и контроллерами при доставке. Изменения этого этапа в кластер не применялись.

## Применение конфигурации Cilium при ручном Sync

При диагностике Service Argo CD уже запрашивал `10.60.80.2`, пул был без конфликта и содержал десять свободных адресов. В ConfigMap L2 announcements был включён, но все три агента показывали `EnableL2Announcements: false`, а оператор был запущен до изменения конфигурации. Chart по умолчанию не перезапускает эти компоненты при изменении ConfigMap.

- [x] Одинаково включить `rollOutCiliumPods: true` и `operator.rollOutPods: true` в Application и bootstrap HelmChartProxy.
- [x] Задать для агентов `RollingUpdate` с `maxUnavailable: 1`; сохранить одну реплику оператора, версии chart, пул, L2-политику, Service Argo CD и исключения сертификатов.
- [x] Обновить инструкции: ручной Sync Cilium запускает rollout при изменении конфигурации, без дополнительных скриптов или hooks.
- [ ] После публикации выполнить ручной Sync `cilium` и дождаться готовности оператора и всех трёх агентов.
- [ ] Подтвердить `EnableL2Announcements: true` на каждом агенте, выдачу `.2` существующему Service, L2 lease и HTTPS-доступ из LAN; проверить Cilium, DNS, межузловую связь и Kubernetes API.

Для существующего Service повторный Sync Argo CD не требуется: нужные настройки уже применены. Изменения этого исправления в кластер не применялись.

Локально подтверждены совпадение Application/bootstrap values, наличие checksum ConfigMap в обоих шаблонах Pod, стабильность checksum при повторном рендере и его изменение при изменении конфигурации. Сравнение с прежними values показывает только добавленные checksum и `maxUnavailable: 1`, за исключением пяти уже игнорируемых полей генерируемых TLS Secrets. Все пять Kustomize-сборок проходят без приватного SSH Secret; 17 bash-примеров из двух README проходят `bash -n`, diff — `git diff --check`. Rollout и назначение IP требуют проверки после доставки в кластер.

## Ограничения и источники

Оба HelmChartProxy используют `InstallOnce`: после установки CAAPH не обновляет релизы при изменении манифестов. Во время move удаление исходных proxy не должно удалять установленные релизы. У HelmReleaseProxy после переноса может не восстановиться Ready, поэтому проверяются работающие компоненты. После передачи управления актуальное состояние показывают отдельные Applications и workloads; Helm status остаётся диагностикой первоначальной установки.

Статическая готовность репозитория не подтверждает доступность сети, загрузку образов, работу контроллеров и успешный pivot. Это проверяется только на живом кластере.

- [Cluster API move](https://cluster-api.sigs.k8s.io/clusterctl/commands/move)
- [CAPI 1.14: изменения API](https://cluster-api.sigs.k8s.io/developer/providers/migrations/v1.13-to-v1.14)
- [k0smotron Remote Machine Provider](https://docs.k0smotron.io/v2.3.0/capi-remote/)
- [CAAPH v0.6.4](https://github.com/kubernetes-sigs/cluster-api-addon-provider-helm/blob/v0.6.4/docs/quick-start.md)
- [Argo CD и Helm](https://argo-cd.readthedocs.io/en/stable/user-guide/helm/)
- [Argo CD RespectIgnoreDifferences](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/#respect-ignore-differences-configs)
