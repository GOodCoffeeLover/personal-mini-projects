# LoadBalancer через Cilium

Существующий ручной Application `cilium` объединяет два источника: Helm chart
`1.20.2` и эту Kustomize-сборку из `main`. Chart включает L2 announcements,
а сборка создаёт общий пул `10.60.80.2–10.60.80.11` включительно и политику
анонса LoadBalancer IP на интерфейсах `en*` и `eth*`. Отдельного Application
для этих ресурсов нет. Sync wave `1` применяет их после готовности ресурсов chart.

Диапазон должен быть свободен и исключён из DHCP. VIP Kubernetes API
`10.60.80.1` и адреса узлов `.101–.103` в пул не входят.
Argo CD запрашивает фиксированный адрес `10.60.80.2` аннотацией
`lbipam.cilium.io/ips`; после его выделения оставшиеся девять адресов доступны
другим LoadBalancer-сервисам. Пул общий, поэтому до выдачи адреса Argo CD
другой сервис может занять `.2`: перед Sync проверьте существующие LB-сервисы.
Это не отдельная резервация IP на уровне пула.

## Bootstrap и передача управления

Cilium использует одинаковые Helm values в Application и bootstrap proxy.
Для Argo CD настройки Service намеренно различаются:

- CAAPH устанавливает Argo CD с `ClusterIP`: Helm может завершить установку
  до создания root Application и пула адресов.
- Ручной Application `argocd` переключает существующий Service на
  `LoadBalancer` с классом `io.cilium/l2-announcer` и `externalTrafficPolicy: Cluster`.

Остальные values Argo CD, версия chart, release name, namespace, Redis и TLS
сохраняются. При обновлении общих values меняйте Application и bootstrap proxy
согласованно; различие Service сохраняйте. CAAPH остаётся в режиме `InstallOnce`.
Настройки пула не входят в CAPI-сборку `cluster/` и не участвуют в pivot.

Для агентов задан `rollOutCiliumPods: true`, для оператора —
`operator.rollOutPods: true`. Chart добавляет checksum ConfigMap в шаблоны Pod:
изменение конфигурации при ручном Sync запускает rollout обоих компонентов.
Агенты обновляются по одному (`RollingUpdate`, `maxUnavailable: 1`), число
реплик оператора остаётся равным одному. Автоматический Sync не включается.

## Порядок применения

1. Завершите текущие работы с control-plane и подтвердите исправность кластера.
   При первой передаче управления проверьте proxy, резервные копии и live diff
   по [основной инструкции](../README.md#передача-cilium-и-argo-cd-под-управление-applications).
2. Опубликуйте изменения в `main`, дождитесь обновления определений Applications
   от root Application. Дочерние Applications остаются с ручным Sync.
3. Просмотрите diff и синхронизируйте `cilium` без prune, Force и Replace.
   В одном Sync сначала применится chart, затем пул и L2-политика.
   Дождитесь завершения rollout оператора и агентов, подтвердите
   `EnableL2Announcements: true` на каждом агенте и отсутствие конфликта пула.
4. Просмотрите diff и синхронизируйте `argocd` без prune, Force и Replace.
   У Service `argocd-server` должен появиться `EXTERNAL-IP: 10.60.80.2`.
5. Проверьте L2 lease и доступ к `https://10.60.80.2` из локальной сети.
   Используется существующая TLS-конфигурация Argo CD; новые сертификаты и DNS
   этим изменением не создаются.

Если Service Argo CD уже запрашивает `.2`, но остаётся в `Pending`, повторный
Sync Argo CD не нужен: после загрузки конфигурации оператор Cilium должен
назначить адрес существующему Service. При диагностике ConfigMap уже содержал
`enable-l2-announcements: true`, но все три агента показывали `false`,
оператор также работал с момента до изменения конфигурации. Без rollout
статус Application `Synced` не подтверждает загрузку настроек процессами.

Команды чтения состояния из корня репозитория:

```bash
export KUBECONFIG="$PWD/lab-cluster/.state/lab-cluster.kubeconfig"
export NO_PROXY="${NO_PROXY},10.60.80.1,10.60.80.2"
kubectl -n kube-system rollout status deployment/cilium-operator --timeout=5m
kubectl -n kube-system rollout status daemonset/cilium --timeout=10m
for pod in $(kubectl -n kube-system get pods -l k8s-app=cilium -o name); do
  echo "$pod"
  kubectl -n kube-system exec "$pod" -c cilium-agent -- cilium-dbg config --all \
    | rg EnableL2Announcements
done
kubectl get ciliumloadbalancerippools
kubectl get ciliuml2announcementpolicies
kubectl -n argocd get service argocd-server
kubectl -n kube-system get leases
```

Подтвердите `EXTERNAL-IP: 10.60.80.2`, lease
`cilium-l2announce-argocd-argocd-server`, доступ к `https://10.60.80.2` из LAN
и исправность Cilium, DNS, межузловой связи и Kubernetes API.

Статическая сборка: `kubectl kustomize lab-cluster/cilium-config`.
Для отката доступа верните `server.service.type: ClusterIP` и удалите
`loadBalancerClass` и запрос IP из Application Argo CD; после ручного Sync
доступ через port-forward снова остаётся доступным. Адреса общего пула
перед удалением его ресурсов нужно освободить от остальных LB-сервисов.

- [Cilium LB IPAM](https://docs.cilium.io/en/stable/network/lb-ipam/)
- [Cilium L2 announcements](https://docs.cilium.io/en/stable/network/l2-announcements/)
- [Argo CD: multiple sources](https://argo-cd.readthedocs.io/en/stable/user-guide/multiple_sources/)
