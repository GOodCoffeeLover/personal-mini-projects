# План bootstrap homelab-кластера на remote machines

## Цель и состояние

Ближайший этап — подготовить репозиторий к запуску `lab-cluster`. Основные манифесты и скрипты написаны; перестройка каталогов завершена. Применение исправлений Redis и запуска GitOps, проверка актуальной конфигурации на кластере и перенос управления остаются отдельными незавершёнными шагами. Команды запуска и ручной диагностики находятся в [README.md](README.md).

Сохраняем исходную архитектуру: временный kind management-кластер создаёт один control-plane узел из пула трёх физических машин. На нём работают Cilium, Argo CD, cert-manager и пять CAPI-провайдеров. Затем управление переносится в целевой кластер вручную. Storage, ingress, VPN, прикладные сервисы и масштабирование control plane входят в следующие этапы.

| Параметр | Решение |
| --- | --- |
| Namespace Cluster, шаблонов, PRM и CAAPH-объектов | Совпадает с `Cluster.metadata.name`: `lab-cluster` |
| Kubernetes и API | `v1.36.5`, `10.60.80.1:6443` через kube-vip |
| Начальный control plane | 1 узел, выбранный из трёх PRM; остальные машины остаются в пуле |
| CAPI core, kubeadm bootstrap/control plane | `v1.14.3` с обеих сторон pivot |
| k0smotron и CAAPH | `v2.3.0` и `v0.6.4` с обеих сторон pivot |
| Helm charts | cert-manager `v1.21.2`, CAPI Operator `0.30.0`, Cilium `1.20.2`, Argo CD `10.9.6` |
| Redis для Argo CD | `docker.io/library/redis:8.6.4-alpine` |
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
4. `lab-cluster-cluster` создаётся заранее, но до pivot его нельзя синхронизировать. Cluster и PRM должны появиться в целевом management-кластере только через move. После переноса приложение также остаётся ручным.
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

## Ограничения и источники

Оба HelmChartProxy используют `InstallOnce`: после установки CAAPH не обновляет релизы при изменении манифестов. Во время move удаление исходных proxy не должно удалять установленные релизы. У HelmReleaseProxy после переноса может не восстановиться Ready, поэтому проверяются сами Helm-релизы и работающие компоненты. Обновление Cilium и Argo CD требует отдельного плана передачи управления.

Статическая готовность репозитория не подтверждает доступность сети, загрузку образов, работу контроллеров и успешный pivot. Это проверяется только на живом кластере.

- [Cluster API move](https://cluster-api.sigs.k8s.io/clusterctl/commands/move)
- [CAPI 1.14: изменения API](https://cluster-api.sigs.k8s.io/developer/providers/migrations/v1.13-to-v1.14)
- [k0smotron Remote Machine Provider](https://docs.k0smotron.io/v2.3.0/capi-remote/)
- [CAAPH v0.6.4](https://github.com/kubernetes-sigs/cluster-api-addon-provider-helm/blob/v0.6.4/docs/quick-start.md)
