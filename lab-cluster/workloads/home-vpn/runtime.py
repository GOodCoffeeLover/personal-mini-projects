#!/usr/bin/env python3
"""Pod-local VPN setup and reconciliation. Python stdlib only.

Never run init/guard on a host. Both verify Downward API addresses first.
No secrets or full Xray configuration are logged.
"""
import argparse
import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path('/settings')
RUNTIME = Path('/runtime')
SOCKET = '/tailscale/tailscaled.sock'
PRIVATE4 = ['0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
            '169.254.0.0/16', '172.16.0.0/12', '192.168.0.0/16',
            '198.18.0.0/15', '224.0.0.0/4', '240.0.0.0/4']
PRIVATE6 = ['::/128', '::1/128', 'fc00::/7', 'fe80::/10', 'ff00::/8']
TAGS = ['tailscale-exit', 'selective-http', 'selective-socks']
READINESS_ERROR = 'readiness-error'
MAX_READINESS_ERROR = 240


def run(*args, input=None):
    result = subprocess.run(args, input=input, text=True, capture_output=True, timeout=8)
    if result.returncode:
        # CLI output might include credentials; only report the executable.
        detail = ': ' + result.stderr.strip()[:400] if args[0] in ('nft', 'ip', 'ss', 'sysctl') else ''
        raise RuntimeError(f'{Path(args[0]).name} failed (exit {result.returncode}){detail}')
    return result.stdout


def ts(*args):
    return run('/tailscale/tailscale', f'--socket={SOCKET}', *args)


def atomic(path, value, mode=0o640):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with open(temporary, 'w', opener=lambda p, f: os.open(p, f, mode)) as stream:
        stream.write(value)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def validate(settings, pod_ip):
    pod = ipaddress.IPv4Address(pod_ip)
    retries = settings.get('tcp_retries2')
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 8:
        raise ValueError('tcp_retries2 must be an integer of at least 8')
    for key in ('pod_cidr', 'service_cidr', 'tailnet_cidr'):
        ipaddress.IPv4Network(settings[key])
    if pod not in ipaddress.ip_network(settings['pod_cidr']):
        raise ValueError('Pod IP outside configured Pod CIDR')
    probe = ipaddress.IPv4Address(settings['return_route_probe'])
    if probe not in ipaddress.ip_network(settings['tailnet_cidr']):
        raise ValueError('Return route probe must be in the tailnet range')
    if not isinstance(settings['advertise_exit_node'], bool):
        raise ValueError('advertise_exit_node must be a boolean')
    if settings['pod_interface'] != 'eth0':
        raise ValueError('Only the verified eth0 Pod interface is supported')
    if settings['tailnet_cidr'] != '100.64.0.0/10':
        raise ValueError('Unexpected tailnet range')
    ipaddress.IPv6Network(settings['tailnet_ipv6_cidr'])
    return str(pod)


def verify_namespace(settings, pod_ip):
    validate(settings, pod_ip)
    if pod_ip == os.environ.get('NODE_IP'):
        raise RuntimeError('Refusing to modify a non-Pod network namespace')
    addresses = json.loads(run('ip', '-j', '-4', 'addr', 'show', 'dev', 'eth0'))
    if not any(a['local'] == pod_ip for item in addresses for a in item['addr_info']):
        raise RuntimeError('Downward API Pod IP does not match eth0')


def nft_rules(settings, pod_ip):
    validate(settings, pod_ip)
    # add+flush+rebuild is ONE nft transaction. No global flush or table delete.
    # A return in these base chains still allows Tailscale's own ACL filtering.
    return f'''add table inet xray_exit
flush table inet xray_exit
table inet xray_exit {{
  chain prerouting {{
    type filter hook prerouting priority mangle; policy accept;
    iifname "tailscale0" ip daddr {settings['tailnet_cidr']} return
    iifname "tailscale0" meta nfproto ipv4 meta l4proto {{ tcp, udp }} tproxy ip to {pod_ip}:12345 meta mark set 0x1 accept
  }}
  chain input {{
    type filter hook input priority -300; policy accept;
    iifname "lo" return
    iifname "tailscale0" meta nfproto ipv6 ip6 daddr != {settings['tailnet_ipv6_cidr']} reject
    iifname "tailscale0" meta mark 0x1 return
    meta l4proto {{ tcp, udp }} th dport 12345 drop
    iifname "tailscale0" ip daddr {settings['tailnet_cidr']} tcp dport 8080 return
    iifname "tailscale0" ip daddr {settings['tailnet_cidr']} meta l4proto {{ tcp, udp }} th dport 1080 return
    tcp dport 8080 drop
    meta l4proto {{ tcp, udp }} th dport 1080 drop
  }}
  chain forward {{
    type filter hook forward priority -300; policy accept;
    iifname "tailscale0" reject
  }}
  chain output {{
    type filter hook output priority filter; policy accept;
    ct state established,related return
    meta skuid 65532 ip daddr {{ {', '.join(PRIVATE4)} }} reject
    meta skuid 65532 ip6 daddr {{ {', '.join(PRIVATE6)} }} reject
  }}
}}
'''


def normalized_nft():
    value = json.loads(run('nft', '-j', 'list', 'table', 'inet', 'xray_exit'))
    def strip(obj):
        if isinstance(obj, dict):
            return {k: strip(v) for k, v in obj.items() if k not in ('handle', 'metainfo')}
        if isinstance(obj, list):
            return [strip(v) for v in obj if not isinstance(v, dict) or 'metainfo' not in v]
        return obj
    return strip(value)


def firewall(settings, pod_ip):
    desired = nft_rules(settings, pod_ip)
    cache = RUNTIME / 'firewall.json'
    try:
        previous = json.loads(cache.read_text())
        if previous['source'] == desired and previous['rules'] == normalized_nft():
            return
    except (OSError, ValueError, RuntimeError, KeyError):
        pass
    run('nft', '--check', '--file', '-', input=desired)
    run('nft', '--file', '-', input=desired)
    atomic(cache, json.dumps({'source': desired, 'rules': normalized_nft()}))
    print('Pod firewall installed/restored atomically', flush=True)


def policy_rules():
    rules = json.loads(run('ip', '-j', '-4', 'rule', 'show'))
    expected = {
        50: {'src': 'all', 'dst': '100.64.0.0/10', 'table': '52'},
        100: {'src': 'all', 'fwmark': '0x1', 'table': '100'},
    }
    additions = []
    for priority, fields in expected.items():
        found = [r for r in rules if r.get('priority') == priority]
        for rule in found:
            comparable = {k: v for k, v in rule.items()
                          if k not in ('priority', 'protocol', 'fwmask')}
            comparable['table'] = str(comparable.get('table'))
            if 'dstlen' in comparable:
                comparable['dst'] = f"{comparable['dst']}/{comparable.pop('dstlen')}"
            if comparable != fields or rule.get('fwmask', '0xffffffff') != '0xffffffff':
                raise RuntimeError(f'Conflicting ip rule priority {priority}; not deleting it')
        if len(found) > 1:
            # Do not remove foreign rules, even if a duplicate looks identical.
            raise RuntimeError(f'Duplicate ip rule priority {priority}')
        if not found:
            additions.append(priority)
    for priority in additions:
        selector = ['to', '100.64.0.0/10'] if priority == 50 else ['fwmark', '0x1/0xffffffff']
        run('ip', '-4', 'rule', 'add', 'priority', str(priority), *selector,
            'lookup', '52' if priority == 50 else '100')
        print(f'Policy rule {priority} restored', flush=True)


def route_table():
    # A new Pod owns table 100. Foreign routes are not overwritten.
    routes = json.loads(run('ip', '-j', '-4', 'route', 'show', 'table', 'all'))
    found = [r for r in routes if str(r.get('table')) == '100']
    if len(found) > 1:
        raise RuntimeError('Multiple routes in table 100; refusing to modify')
    for route in found:
        if route.get('type') != 'local' or route.get('dst') != 'default' or route.get('dev') != 'lo':
            raise RuntimeError('Unexpected route in table 100; refusing to overwrite')
    if not found:
        run('ip', '-4', 'route', 'add', 'local', 'default', 'dev', 'lo', 'table', '100')
        print('Local TPROXY route restored', flush=True)


def reconcile_tcp_retries2(settings, force=False):
    desired = settings.get('tcp_retries2')
    if isinstance(desired, bool) or not isinstance(desired, int) or desired < 8:
        raise ValueError('tcp_retries2 must be an integer of at least 8')
    key = 'net.ipv4.tcp_retries2'
    if force or run('sysctl', '-n', key).strip() != str(desired):
        run('sysctl', '-w', f'{key}={desired}')
        print(f'Pod {key} set to {desired}', flush=True)


def tailscale_status():
    status = json.loads(ts('status', '--json'))
    if status.get('BackendState') != 'Running':
        raise RuntimeError('Tailscale is not Running')
    if not isinstance(status.get('Self'), dict) or status['Self'].get('Online') is not True:
        raise RuntimeError('Tailscale is not online')
    health = status.get('Health') or []
    if not isinstance(health, list) or not all(isinstance(item, str) for item in health):
        raise RuntimeError('Unexpected Tailscale health status')
    for warning in health:
        normalized = warning.lower().replace('_', '-').replace(' ', '-')
        coordination_loss = ('coordination-server' in normalized and
                             any(word in normalized for word in ('unable', 'cannot', 'lost', 'disconnect', 'offline')))
        if 'not-in-map-poll' in normalized or coordination_loss:
            raise RuntimeError('Tailscale lost its coordination server connection')
    return status


def tailnet_ip(status=None):
    status = tailscale_status() if status is None else status
    ips = [ip for ip in status.get('TailscaleIPs', [])
           if ipaddress.ip_address(ip).version == 4]
    if len(ips) != 1 or ipaddress.ip_address(ips[0]) not in ipaddress.ip_network('100.64.0.0/10'):
        raise RuntimeError('Expected one tailnet IPv4')
    return ips[0]


def xray_config(settings, pod_ip, tail_ip, upstream, direct_domains):
    validate(settings, pod_ip)
    if ipaddress.IPv4Address(tail_ip) not in ipaddress.ip_network(settings['tailnet_cidr']):
        raise ValueError('Invalid tailnet IP')
    if upstream.get('tag') != 'upstream' or upstream.get('protocol') != 'vless':
        raise ValueError('Expected dedicated upstream VLESS outbound')
    upstream = copy.deepcopy(upstream)
    # A literal public IPv4 avoids DNS bootstrap loops for the tunnel itself.
    endpoint = upstream['settings']['vnext'][0]
    if not ipaddress.IPv4Address(endpoint['address']).is_global:
        raise ValueError('upstream endpoint must be a public IPv4')
    if 'REPLACE_' in json.dumps(upstream):
        raise ValueError('upstream credentials are still placeholders')
    if not isinstance(direct_domains, list) or not all(
            isinstance(domain, str) and domain.strip()
            for domain in direct_domains):
        raise ValueError('Expected a list of nonempty Xray domain selectors')
    sniff = {'enabled': True, 'destOverride': ['http', 'tls', 'quic'], 'routeOnly': True}
    inbounds = [{
        'tag': 'tailscale-exit', 'listen': pod_ip, 'port': 12345,
        'protocol': 'tunnel', 'settings': {'network': 'tcp,udp', 'followRedirect': True},
        'sniffing': sniff, 'streamSettings': {'sockopt': {'tproxy': 'tproxy'}},
    }]
    for prefix, listen, relay in [('selective', tail_ip, tail_ip)]:
        inbounds += [
            {'tag': prefix + '-http', 'listen': listen, 'port': 8080, 'protocol': 'http',
             'settings': {'allowTransparent': False}, 'sniffing': sniff},
            {'tag': prefix + '-socks', 'listen': listen, 'port': 1080, 'protocol': 'socks',
             'settings': {'auth': 'noauth', 'udp': True, 'ip': relay}, 'sniffing': sniff},
        ]
    def rule(outbound, **match):
        return {'type': 'field', 'inboundTag': TAGS, **match, 'outboundTag': outbound}
    return {
        'log': {'loglevel': 'warning', 'access': 'none'},
        'dns': {'servers': [settings['xray_dns']], 'queryStrategy': 'UseIP', 'tag': 'dns-egress'},
        'inbounds': inbounds,
        # Default outbound is block, never accidental direct.
        'outbounds': [{'tag': 'block', 'protocol': 'blackhole'}, upstream,
                      {'tag': 'direct', 'protocol': 'freedom',
                       'settings': {'domainStrategy': 'UseIPv4'}}],
        'routing': {'domainStrategy': 'IPOnDemand', 'rules': [
            {'type': 'field', 'inboundTag': ['dns-egress'], 'outboundTag': 'upstream'},
            rule('block', ip=['geoip:private', *PRIVATE4, *PRIVATE6,
                             settings['pod_cidr'], settings['service_cidr']]),
            *([rule('direct', domain=direct_domains)] if direct_domains else []),
            rule('direct', ip=['geoip:ru', 'geoip:by']),
            rule('upstream', network='tcp,udp'), rule('block'),
        ]},
    }


def configure(settings, pod_ip):
    verify_namespace(settings, pod_ip)
    tail_ip = tailnet_ip()
    config = xray_config(settings, pod_ip, tail_ip,
                         json.loads(Path('/upstream/outbound.json').read_text()),
                         json.loads((ROOT / 'direct-domains.json').read_text()))
    atomic(RUNTIME / 'xray.json', json.dumps(config))
    os.chown(RUNTIME / 'xray.json', 0, 65532)
    atomic(RUNTIME / 'addresses.json', json.dumps({'pod_ip': pod_ip, 'tail_ip': tail_ip}))
    print('Xray configuration generated (credentials not logged)', flush=True)


def init(settings, pod_ip):
    verify_namespace(settings, pod_ip)
    firewall(settings, pod_ip)
    for key, value in [('net.ipv4.ip_forward', '1'), ('net.ipv6.conf.all.forwarding', '1'),
                       ('net.ipv4.conf.all.rp_filter', '0'), ('net.ipv4.conf.default.rp_filter', '0'),
                       ('net.ipv4.conf.eth0.rp_filter', '0')]:
        run('sysctl', '-w', f'{key}={value}')
    reconcile_tcp_retries2(settings, force=True)
    route_table()
    policy_rules()


def listeners(pod_ip, tail_ip):
    for transport, expected in [
        ('-t', {(tail_ip, str(port)) for port in (8080, 1080)} | {(pod_ip, '12345')}),
        ('-u', {(tail_ip, '1080')} | {(pod_ip, '12345')}),
    ]:
        lines = run('ss', '-H', '-l', '-n', '-4', transport).splitlines()
        actual = set()
        for line in lines:
            address = line.split()[3]
            host, port = address.rsplit(':', 1)
            actual.add((host, port))
        proxy = {(host, port) for host, port in actual if port in ('8080', '1080', '12345')}
        if proxy != expected:
            raise RuntimeError('Xray listeners are missing or exposed outside the tailnet')


def advertise(enabled):
    prefs = json.loads(ts('debug', 'prefs'))
    expected = ['0.0.0.0/0', '::/0'] if enabled else []
    if sorted(prefs.get('AdvertiseRoutes') or []) != sorted(expected):
        ts('set', '--advertise-routes=', '--advertise-exit-node=' + str(enabled).lower())
        print(f'Exit advertisement set to {enabled}', flush=True)


def publish_ready(settings):
    advertise(settings['advertise_exit_node'])
    atomic(RUNTIME / 'ready', str(time.monotonic()))
    (RUNTIME / READINESS_ERROR).unlink(missing_ok=True)


def withdraw_ready():
    (RUNTIME / 'ready').unlink(missing_ok=True)
    try:
        advertise(False)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
        pass  # Firewall stays in place even when the local socket is unavailable.


def readiness_reason(error):
    reason = type(error).__name__ + ': ' + str(error)
    reason = ' '.join(reason.split())
    reason = re.sub(r'(?i)\bauthorization\b\s*[:=]\s*(?:bearer\s+)?\S+',
                    'authorization=<redacted>', reason)
    reason = re.sub(r'(?i)\b(token|authkey|password|secret)\b\s*[:=]\s*\S+',
                    r'\1=<redacted>', reason)
    reason = re.sub(r'(?i)\bbearer\s+\S+', 'Bearer <redacted>', reason)
    reason = re.sub(r'(?i)\btskey-[^\s]+', '<redacted>', reason)
    return reason[:MAX_READINESS_ERROR] or 'readiness check failed'


def probe(kind):
    file = RUNTIME / ('ready' if kind == 'ready' else 'heartbeat')
    try:
        age = time.monotonic() - float(file.read_text())
        healthy = 0 <= age < (20 if kind == 'ready' else 90)
    except (OSError, ValueError):
        healthy = False
    if healthy:
        return 0
    if kind == 'ready':
        try:
            reason = (RUNTIME / READINESS_ERROR).read_text().strip()
        except OSError:
            reason = 'guard has not reported readiness'
        print(('Not ready: ' + reason)[:MAX_READINESS_ERROR], file=sys.stderr)
    return 1


def reconcile(settings, pod_ip):
    verify_namespace(settings, pod_ip)
    firewall(settings, pod_ip)
    route_table()
    policy_rules()
    reconcile_tcp_retries2(settings)
    for path in ('net/ipv4/ip_forward', 'net/ipv6/conf/all/forwarding'):
        if (Path('/proc/sys') / path).read_text().strip() != '1':
            raise RuntimeError('Pod forwarding sysctl changed; recreate Pod')
    tail_ip = tailnet_ip()
    recorded = json.loads((RUNTIME / 'addresses.json').read_text())
    if recorded != {'pod_ip': pod_ip, 'tail_ip': tail_ip}:
        raise RuntimeError('Address changed; recreate Pod to regenerate Xray configuration')
    route = json.loads(run('ip', '-j', '-4', 'route', 'get', settings['return_route_probe']))
    if not route or route[0].get('dev') != 'tailscale0':
        raise RuntimeError('Tailnet return route missing')
    listeners(pod_ip, tail_ip)


def guard(settings, pod_ip):
    verify_namespace(settings, pod_ip)
    (RUNTIME / 'ready').unlink(missing_ok=True)
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    failure = None
    while not stopping:
        atomic(RUNTIME / 'heartbeat', str(time.monotonic()))
        try:
            reconcile(settings, pod_ip)
            publish_ready(settings)
            if failure is not None:
                print('VPN local readiness restored', flush=True)
            failure = None
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
            withdraw_ready()
            reason = readiness_reason(error)
            atomic(RUNTIME / READINESS_ERROR, reason)
            if reason != failure:
                print('Not ready: ' + reason, flush=True)
            failure = reason
        for _ in range(10):
            if stopping:
                break
            time.sleep(0.5)
    withdraw_ready()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['init', 'configure', 'guard', 'ready', 'alive', 'print-nft'])
    args = parser.parse_args()
    if args.mode in ('ready', 'alive'):
        sys.exit(probe(args.mode))
    settings = json.loads((ROOT / 'settings.json').read_text())
    pod_ip = os.environ['POD_IP']
    if args.mode == 'print-nft':
        print(nft_rules(settings, pod_ip))
    else:
        {'init': init, 'configure': configure, 'guard': guard}[args.mode](settings, pod_ip)
