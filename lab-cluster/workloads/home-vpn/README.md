# home-vpn

VPN в namespace `home-vpn` кластера `lab-cluster`: Tailscale обеспечивает доступ через tailnet, Xray обрабатывает exit-трафик и HTTP/SOCKS-прокси. Одна реплика, стратегия `Recreate`, сеть Pod без `hostNetwork`, узлы `linux/amd64`. Версии и digest образов закреплены в Deployment.

## Файлы в Git

| Путь | Содержимое |
| --- | --- |
| [kustomization.yaml](kustomization.yaml) | Сборка ресурсов и генератор ConfigMap `home-vpn-config-<hash>` с кодом runtime |
| [deployment.yaml](deployment.yaml) | Контейнеры, образы, права, volumes, probes и ресурсы Pod |
| [runtime.py](runtime.py) | Настройка TPROXY, policy routing и nftables, генерация конфигурации Xray, guard и проверки готовности |
| [namespace.yaml](namespace.yaml) | Namespace и Pod Security labels для сетевых контейнеров |
| [serviceaccount.yaml](serviceaccount.yaml), [role.yaml](role.yaml), [rolebinding.yaml](rolebinding.yaml) | ServiceAccount и ограниченный доступ Tailscale к state Secret и Events |
| [examples/](examples/) | Синтетические настройки runtime, список direct-доменов, hostname, аргументы Tailscale и шаблон outbound |
| [settings.py](settings.py) | Выгрузка и загрузка редактируемых настроек через kubectl; после загрузки — restart Deployment |
| [tests/test_runtime.py](tests/test_runtime.py) | Офлайн-тесты маршрутизации, DNS, firewall, listeners, readiness и рекламы exit node |
| [tests/test_manifests.py](tests/test_manifests.py) | Офлайн-проверки Kustomize, прав контейнеров, RBAC, ссылок на Secrets и Application |
| [tests/test_settings.py](tests/test_settings.py) | Офлайн-тесты экспорта, импорта, ошибок API и условий restart |
| [socks_udp_check.py](socks_udp_check.py) | Ручная клиентская проверка SOCKS5 UDP Associate и DNS |
| [../../applications/home-vpn.yaml](../../applications/home-vpn.yaml) | Argo CD Application: источник `main`, ручной Sync, server-side apply, `Prune=false`, без каскадного finalizer |

## Компоненты и доступ

Init-контейнер `network-init` настраивает сеть Pod. Tailscale работает как native sidecar. `xray-configure` читает внешние настройки и создаёт `/runtime/xray.json`, затем `xray-validate` проверяет его до запуска Xray. Каталог `/runtime` хранится в memory-backed `emptyDir`.

`network-guard` восстанавливает собственные nft-правила и policy routing, проверяет маршруты, listeners и связь Tailscale с control plane. При успешных проверках включает readiness и рекламу exit node; при ошибке снимает их. Readiness отражает локальную готовность, доступность upstream проверяется клиентским трафиком.

Доступ через IPv4 Tailscale:

| Назначение | Порт / протокол |
| --- | --- |
| HTTP-прокси | `8080/TCP` |
| SOCKS-прокси | `1080/TCP`, `1080/UDP` |
| Exit node | IPv4 TCP/UDP через TPROXY, внутренний listener `12345` |

Доступ к HTTP/SOCKS регулируют tailnet ACL; отдельной авторизации прокси нет. Service и LAN listeners отсутствуют. IPv6 вне tailnet и неперехваченный forwarding отклоняются.

В Xray default outbound — `block`. DNS идёт через `upstream`; приватные, Pod и Service сети блокируются. Direct-исключения задают список доменов из Secret и GeoIP RU/BY. Пустой список доменов отключает доменные исключения. Для остального трафика при отказе upstream нет fallback в direct.

## Настройки и состояние вне Git

Все три Secret находятся в namespace `home-vpn`, создаются отдельно и не входят в сборку Argo CD.

| Secret | Ключи | Где используется |
| --- | --- | --- |
| `home-vpn-settings` | `settings.json`, `direct-domains.json` | `/settings` в `network-init`, `xray-configure` и `network-guard` |
| `home-vpn-settings` | `TS_HOSTNAME`, `TS_EXTRA_ARGS` | Переменные окружения sidecar Tailscale через `secretKeyRef` |
| `home-vpn-upstream` | `outbound.json` | `/upstream/outbound.json`, только в `xray-configure` |
| `home-vpn-state` | Состояние Tailscale и ключи identity | Kubernetes state backend Tailscale через API |

`settings.json` содержит Pod/Service CIDR, tailnet CIDR, интерфейс Pod, `tcp_retries2`, DNS, `return_route_probe` и флаг `advertise_exit_node`. `direct-domains.json` содержит селекторы Xray, включая `domain:`, `full:`, `keyword:`, `regexp:`, `geosite:` и строки без префикса; допустим `[]`. Их синтаксис и наличие наборов данных проверяет `xray-validate`.

`outbound.json` содержит полный VLESS outbound с tag `upstream` и публичным IPv4 endpoint. Runtime сохраняет его Reality/TLS-параметры без подмены. В `TS_EXTRA_ARGS` находятся адрес control plane, `--accept-routes=false` и `--advertise-exit-node=false`; рекламу включает guard.

API token смонтирован только в Tailscale. ServiceAccount имеет `get/update/patch` только для `home-vpn-state` и необходимые права на Events. Tailscale использует существующую identity без `TS_AUTHKEY`; одновременно работает один экземпляр с этим state.

Локальные пути относительно корня репозитория:

| Путь | Содержимое |
| --- | --- |
| `lab-cluster/workloads/home-vpn/settings.local/` | Пять редактируемых файлов из `home-vpn-settings` и `home-vpn-upstream` |
| `.state/home-vpn/backups/` | Закрытые снимки ресурсов, Secrets и состояния |
| `lab-cluster/.state/lab-cluster.kubeconfig` | Kubeconfig целевого кластера |

Эти каталоги исключены из Git. Отдельная закрытая резервная копия хранится в `~/home-vpn-backups/`. Примеры в Git содержат заглушки и синтетические CIDR; реальные адреса, UUID, ключи и серверные параметры находятся в локальных файлах и Secrets.

## Редактирование настроек

Нужны Python 3 и kubectl с доступом к Secrets и Deployment. Из корня репозитория выгрузите текущие настройки:

```bash
python3 lab-cluster/workloads/home-vpn/settings.py export
```

В `settings.local/` рядом со скриптом появятся `settings.json`, `direct-domains.json`, `TS_HOSTNAME`, `TS_EXTRA_ARGS` и `outbound.json`. Файлы содержат декодированные значения и доступны владельцу (`0600`, каталог `0700`). Повторный `export` заменяет эти пять файлов значениями из кластера; ошибочный ответ API или отсутствие ключа сохраняет предыдущую выгрузку. Каталог исключён из Git и не входит в Kustomize. `home-vpn-state` скрипт не читает и не изменяет.

Отредактируйте нужные файлы и загрузите весь комплект:

```bash
python3 lab-cluster/workloads/home-vpn/settings.py import
```

Перед обращением к API проверяются наличие всех файлов, UTF-8, синтаксис и базовая структура JSON. Скрипт создаёт отсутствующие конфигурационные Secrets или обновляет только соответствующие ключи существующих, сохраняя остальные данные и метаданные. Затем выполняет restart Deployment `home-vpn` и ждёт rollout до 10 минут. Ноль реплик сохраняется; отсутствующий Deployment не создаётся. При приостановленном Deployment загрузка отказывается от изменений. Проверки полного runtime-конфига и Xray выполняются при запуске Pod.

Если обновление одного из Secrets завершается ошибкой, restart не запрашивается; сообщение указывает уже изменённые Secrets. Если ошибка возникает при restart или ожидании rollout, загруженные настройки остаются в кластере, скрипт завершается с ошибкой без автоматического отката. Значения Secrets и необработанный вывод ошибок kubectl в терминал не попадают.

По умолчанию используются namespace `home-vpn`, kubeconfig `lab-cluster/.state/lab-cluster.kubeconfig` и каталог рядом со скриптом, независимо от рабочей директории. Для обеих команд доступны `--kubeconfig`, `--namespace` и `--directory`, например:

```bash
python3 lab-cluster/workloads/home-vpn/settings.py export \
  --kubeconfig /path/to/kubeconfig --namespace home-vpn --directory /path/to/settings
```

Вызовы kubectl выполняются без переменных HTTP/HTTPS/ALL_PROXY окружения: прямое соединение с API должно сохраняться при restart самого VPN.
