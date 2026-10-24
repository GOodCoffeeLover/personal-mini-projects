import copy
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime as vpn

HERE = Path(__file__).resolve().parents[1]
EXAMPLES = HERE / 'examples'
SETTINGS = json.loads((EXAMPLES / 'settings.json').read_text())
DOMAINS = json.loads((EXAMPLES / 'direct-domains.json').read_text())
POD_IP = '10.244.1.23'
TAIL_IP = '100.64.0.123'


def upstream():
    outbound = json.loads((EXAMPLES / 'outbound.json').read_text())
    outbound['settings']['vnext'][0]['address'] = '93.184.216.34'
    user = outbound['settings']['vnext'][0]['users'][0]
    user['flow'] = ''
    user['id'] = '00000000-0000-4000-8000-000000000001'
    outbound['streamSettings']['realitySettings'].update(
        fingerprint='chrome', serverName='example.com', publicKey='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA', shortId='ab')
    return outbound


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.config = vpn.xray_config(SETTINGS, POD_IP, TAIL_IP, upstream(), DOMAINS)

    def test_three_distinct_inbounds(self):
        self.assertEqual(set(vpn.TAGS), {x['tag'] for x in self.config['inbounds']})
        by_tag = {x['tag']: x for x in self.config['inbounds']}
        self.assertEqual(by_tag['selective-socks']['listen'], TAIL_IP)
        self.assertEqual(by_tag['selective-socks']['settings']['ip'], TAIL_IP)
        self.assertEqual(by_tag['tailscale-exit']['listen'], POD_IP)
        self.assertEqual(by_tag['tailscale-exit']['settings']['network'], 'tcp,udp')

    def test_fail_closed_and_dns(self):
        self.assertEqual(self.config['outbounds'][0]['tag'], 'block')
        self.assertEqual(self.config['dns']['servers'], ['https+local://1.1.1.1/dns-query'])
        rules = self.config['routing']['rules']
        self.assertEqual(self.config['routing']['domainStrategy'], 'IPOnDemand')
        self.assertEqual(rules[0]['inboundTag'], ['dns-egress'])
        self.assertEqual(rules[0]['outboundTag'], 'upstream')
        self.assertEqual(rules[1]['outboundTag'], 'block')
        self.assertIn(SETTINGS['service_cidr'], rules[1]['ip'])
        self.assertEqual(rules[2]['domain'], DOMAINS)
        self.assertNotIn('ip', rules[2])
        self.assertNotIn('domain', rules[3])
        self.assertEqual(rules[-2]['outboundTag'], 'upstream')
        self.assertEqual(rules[-1]['outboundTag'], 'block')
        self.assertTrue(all(r['inboundTag'] == vpn.TAGS for r in rules[1:]))

    def test_full_upstream_is_preserved(self):
        source = upstream()
        self.assertEqual(source['streamSettings']['realitySettings']['fingerprint'], 'chrome')
        config = vpn.xray_config(SETTINGS, POD_IP, TAIL_IP, source, DOMAINS)
        self.assertEqual(config['outbounds'][1], source)

    def test_empty_domain_list_does_not_create_a_direct_catchall(self):
        config = vpn.xray_config(SETTINGS, POD_IP, TAIL_IP, upstream(), [])
        direct = [r for r in config['routing']['rules'] if r['outboundTag'] == 'direct']
        self.assertEqual(len(direct), 1)
        self.assertEqual(direct[0]['ip'], ['geoip:ru', 'geoip:by'])

    def test_xray_domain_selector_formats_are_preserved(self):
        domains = ['domain:example.com', 'full:example.org', 'keyword:example',
                   'regexp:example[.]net$', 'geosite:category-ads-all', 'example.net']
        config = vpn.xray_config(SETTINGS, POD_IP, TAIL_IP, upstream(), domains)
        self.assertEqual(config['routing']['rules'][2]['domain'], domains)
        for invalid in ('example.com', [None], [''], ['   ']):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                vpn.xray_config(SETTINGS, POD_IP, TAIL_IP, upstream(), invalid)

    def test_route_probe_must_be_a_tailnet_address(self):
        for value in ('example.com', '192.0.2.1', '::1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                vpn.validate({**SETTINGS, 'return_route_probe': value}, POD_IP)

    def test_rejects_placeholder_and_private_endpoint(self):
        outbound = json.loads((EXAMPLES / 'outbound.json').read_text())
        with self.assertRaises(ValueError):
            vpn.xray_config(SETTINGS, POD_IP, TAIL_IP, outbound, DOMAINS)
        outbound = upstream()
        outbound['settings']['vnext'][0]['address'] = '192.168.1.254'
        with self.assertRaises(ValueError):
            vpn.xray_config(SETTINGS, POD_IP, TAIL_IP, outbound, DOMAINS)

    def test_firewall_owns_only_its_table(self):
        rules = vpn.nft_rules(SETTINGS, POD_IP)
        self.assertTrue(rules.startswith('add table inet xray_exit\nflush table inet xray_exit\n'))
        self.assertNotIn('flush ruleset', rules)
        self.assertNotIn('delete table', rules)
        self.assertIn('iifname "tailscale0" reject', rules)
        self.assertNotIn('iifname "eth0" ip saddr', rules)
        self.assertIn('meta skuid 65532', rules)
        self.assertLess(rules.index('ip daddr 100.64.0.0/10 return'), rules.index('tproxy ip'))

    def test_host_network_refused(self):
        with self.assertRaises(ValueError):
            vpn.validate(SETTINGS, '192.0.2.200')
        with patch.dict(os.environ, {'NODE_IP': POD_IP}):
            with self.assertRaises(RuntimeError):
                vpn.verify_namespace(SETTINGS, POD_IP)

    def test_tcp_retries2_validation(self):
        for value in (True, 7, 8.0, '8', None):
            settings = {**SETTINGS, 'tcp_retries2': value}
            with self.subTest(value=value), self.assertRaises(ValueError):
                vpn.validate(settings, POD_IP)
        self.assertEqual(vpn.validate({**SETTINGS, 'tcp_retries2': 8}, POD_IP), POD_IP)


class PolicyTest(unittest.TestCase):
    def test_tcp_retries2_is_installed_and_restored(self):
        with patch.object(vpn, 'run', return_value='') as run:
            vpn.reconcile_tcp_retries2(SETTINGS, force=True)
            self.assertEqual(run.call_args.args,
                             ('sysctl', '-w', 'net.ipv4.tcp_retries2=8'))
        with patch.object(vpn, 'run', return_value='8\n') as run:
            vpn.reconcile_tcp_retries2(SETTINGS)
            self.assertEqual(run.call_count, 1)
        with patch.object(vpn, 'run', side_effect=['15\n', '']) as run:
            vpn.reconcile_tcp_retries2(SETTINGS)
            self.assertEqual(run.call_args_list[1].args,
                             ('sysctl', '-w', 'net.ipv4.tcp_retries2=8'))

    def test_tailscale_status_requires_control_plane_connection(self):
        healthy = {'BackendState': 'Running', 'Self': {'Online': True},
                   'Health': [], 'TailscaleIPs': [TAIL_IP]}
        with patch.object(vpn, 'ts', return_value=json.dumps(healthy)):
            self.assertEqual(vpn.tailnet_ip(), TAIL_IP)
        unhealthy = [
            {**healthy, 'BackendState': 'Starting'},
            {**healthy, 'Self': {'Online': False}},
            {**healthy, 'Health': ['not-in-map-poll']},
            {**healthy, 'Health': ['Tailscale is unable to connect to the coordination server']},
        ]
        for status in unhealthy:
            with self.subTest(status=status), patch.object(vpn, 'ts', return_value=json.dumps(status)):
                with self.assertRaises(RuntimeError):
                    vpn.tailnet_ip()
        with patch.object(vpn, 'ts', return_value=json.dumps(
                {**healthy, 'Health': ['an unrelated warning']})):
            self.assertEqual(vpn.tailnet_ip(), TAIL_IP)

    def test_ready_and_exit_advertisement_follow_health(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(vpn, 'RUNTIME', Path(directory)), \
                patch.object(vpn, 'advertise') as advertise:
            vpn.publish_ready(SETTINGS)
            self.assertTrue((Path(directory) / 'ready').exists())
            advertise.assert_called_once_with(True)
            advertise.reset_mock()
            vpn.withdraw_ready()
            self.assertFalse((Path(directory) / 'ready').exists())
            advertise.assert_called_once_with(False)

    def test_firewall_repair_is_one_transaction(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(vpn, 'RUNTIME', Path(directory)), \
                patch.object(vpn, 'normalized_nft', return_value={'nftables': []}), \
                patch.object(vpn, 'run', return_value='') as run:
            vpn.firewall(SETTINGS, POD_IP)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args, ('nft', '--check', '--file', '-'))
            self.assertEqual(run.call_args_list[1].args, ('nft', '--file', '-'))
            transaction = run.call_args_list[1].kwargs['input']
            self.assertIn('flush table inet xray_exit', transaction)
            self.assertIn('iifname "tailscale0" reject', transaction)
            run.reset_mock()
            vpn.firewall(SETTINGS, POD_IP)
            run.assert_not_called()
            with patch.object(vpn, 'normalized_nft', return_value={'nftables': ['drift']}):
                vpn.firewall(SETTINGS, POD_IP)
            self.assertEqual(run.call_count, 2)

    def test_advertise_changes_only_when_needed(self):
        with patch.object(vpn, 'ts', return_value='{"AdvertiseRoutes": []}') as ts:
            vpn.advertise(False)
            self.assertEqual(ts.call_count, 1)
        with patch.object(vpn, 'ts', side_effect=['{"AdvertiseRoutes": ["0.0.0.0/0", "::/0"]}', '']) as ts:
            vpn.advertise(False)
            self.assertEqual(ts.call_args.args, ('set', '--advertise-routes=', '--advertise-exit-node=false'))

    def test_missing_rules_added(self):
        with patch.object(vpn, 'run', side_effect=['[]', '', '']) as run:
            vpn.policy_rules()
            self.assertEqual(run.call_count, 3)
            self.assertTrue(all('add' in c.args for c in run.call_args_list[1:]))

    def test_existing_rules_unchanged(self):
        rules = [{'priority': 50, 'src': 'all', 'dst': '100.64.0.0', 'dstlen': 10, 'table': '52'},
                 {'priority': 100, 'src': 'all', 'fwmark': '0x1', 'table': 100}]
        with patch.object(vpn, 'run', return_value=json.dumps(rules)) as run:
            vpn.policy_rules()
            self.assertEqual(run.call_count, 1)

    def test_conflicts_not_deleted(self):
        for extra in [{'table': 'main'}, {'iif': 'eth0'}, {'fwmask': '0xff'}]:
            rule = {'priority': 100, 'src': 'all', 'fwmark': '0x1', 'table': 100, **extra}
            with self.subTest(extra=extra), patch.object(vpn, 'run', return_value=json.dumps([rule])) as run:
                with self.assertRaises(RuntimeError):
                    vpn.policy_rules()
                self.assertEqual(run.call_count, 1)

    def test_duplicate_not_deleted(self):
        rule = {'priority': 50, 'src': 'all', 'dst': '100.64.0.0/10', 'table': 52}
        with patch.object(vpn, 'run', return_value=json.dumps([rule, rule])) as run:
            with self.assertRaises(RuntimeError):
                vpn.policy_rules()
            self.assertEqual(run.call_count, 1)

    def test_foreign_route_not_overwritten(self):
        with patch.object(vpn, 'run', return_value='[{"table":"100","dst":"default","dev":"eth0"}]') as run:
            with self.assertRaises(RuntimeError):
                vpn.route_table()
            self.assertEqual(run.call_count, 1)

    def test_listener_parse(self):
        tcp = '\n'.join(f'LISTEN 0 4096 {ip}:{port} 0.0.0.0:*' for ip in (TAIL_IP,) for port in (1080, 8080))
        tcp += f'\nLISTEN 0 4096 {POD_IP}:12345 0.0.0.0:*'
        udp = '\n'.join(f'UNCONN 0 0 {ip}:1080 0.0.0.0:*' for ip in (TAIL_IP,))
        udp += f'\nUNCONN 0 0 {POD_IP}:12345 0.0.0.0:*'
        with patch.object(vpn, 'run', side_effect=[tcp, udp]):
            vpn.listeners(POD_IP, TAIL_IP)
        with patch.object(vpn, 'run', return_value=''):
            with self.assertRaises(RuntimeError):
                vpn.listeners(POD_IP, TAIL_IP)
        for host in (POD_IP, '0.0.0.0'):
            extra = f'\nLISTEN 0 4096 {host}:8080 0.0.0.0:*'
            with patch.object(vpn, 'run', side_effect=[tcp + extra, udp]):
                with self.assertRaises(RuntimeError):
                    vpn.listeners(POD_IP, TAIL_IP)
        with patch.object(vpn, 'run', side_effect=[tcp, f'UNCONN 0 0 {POD_IP}:12345 0.0.0.0:*']):
            with self.assertRaises(RuntimeError):
                vpn.listeners(POD_IP, TAIL_IP)

    def test_reconcile_uses_configured_return_route_and_rejects_drift(self):
        with contextlib.ExitStack() as stack:
            for method in ('verify_namespace', 'firewall', 'route_table',
                           'policy_rules', 'reconcile_tcp_retries2'):
                stack.enter_context(patch.object(vpn, method))
            stack.enter_context(patch.object(vpn, 'tailnet_ip', return_value=TAIL_IP))
            stack.enter_context(patch.object(Path, 'read_text', side_effect=[
                '1', '1', json.dumps({'pod_ip': POD_IP, 'tail_ip': TAIL_IP})] * 2))
            run = stack.enter_context(patch.object(vpn, 'run', side_effect=[
                '[{"dev":"tailscale0"}]', '[{"dev":"eth0"}]']))
            listeners = stack.enter_context(patch.object(vpn, 'listeners'))
            settings = {**SETTINGS, 'return_route_probe': '100.64.0.201'}
            vpn.reconcile(settings, POD_IP)
            run.assert_called_with('ip', '-j', '-4', 'route', 'get', '100.64.0.201')
            listeners.assert_called_once_with(POD_IP, TAIL_IP)
            with self.assertRaisesRegex(RuntimeError, 'return route missing'):
                vpn.reconcile(settings, POD_IP)

    def test_guard_withdraws_on_failure_and_restores_when_healthy(self):
        handlers = {}
        sleeps = 0
        def sleep(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 20:
                handlers[vpn.signal.SIGTERM]()
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(vpn, 'RUNTIME', Path(directory)))
            stack.enter_context(patch.object(vpn, 'verify_namespace'))
            stack.enter_context(patch.object(vpn.signal, 'signal', side_effect=lambda sig, fn: handlers.update({sig: fn})))
            stack.enter_context(patch.object(vpn.time, 'sleep', side_effect=sleep))
            stack.enter_context(patch.object(vpn, 'reconcile', side_effect=[RuntimeError('rules changed'), None]))
            advertise = stack.enter_context(patch.object(vpn, 'advertise'))
            vpn.guard(SETTINGS, POD_IP)
            self.assertEqual([call.args for call in advertise.call_args_list], [(False,), (True,), (False,)])
            self.assertTrue((Path(directory) / 'heartbeat').exists())
            self.assertFalse((Path(directory) / 'ready').exists())
            self.assertFalse((Path(directory) / vpn.READINESS_ERROR).exists())

    def test_readiness_expiry(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(vpn, 'RUNTIME', Path(directory)):
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(vpn.probe('ready'), 1)
            self.assertEqual(stderr.getvalue().strip(),
                             'Not ready: guard has not reported readiness')
            with patch.object(vpn.time, 'monotonic', return_value=100):
                vpn.atomic(Path(directory) / 'ready', '99')
                self.assertEqual(vpn.probe('ready'), 0)
                vpn.atomic(Path(directory) / 'ready', '70')
                vpn.atomic(Path(directory) / vpn.READINESS_ERROR,
                           'RuntimeError: Tailscale is not online')
                with contextlib.redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(vpn.probe('ready'), 1)
                self.assertEqual(stderr.getvalue().strip(),
                                 'Not ready: RuntimeError: Tailscale is not online')

    def test_readiness_error_is_bounded_and_redacted_then_cleared(self):
        sensitive = RuntimeError(
            'coordination failed\nAuthorization: Bearer abc token=def '
            'authkey=tskey-auth-secret password=hunter2 ' + 'x' * 400)
        reason = vpn.readiness_reason(sensitive)
        self.assertLessEqual(len(reason), vpn.MAX_READINESS_ERROR)
        for value in ('abc', 'def', 'tskey-auth-secret', 'hunter2', '\n'):
            self.assertNotIn(value, reason)
        with tempfile.TemporaryDirectory() as directory, patch.object(vpn, 'RUNTIME', Path(directory)), \
                patch.object(vpn, 'advertise'):
            vpn.atomic(Path(directory) / vpn.READINESS_ERROR, reason)
            vpn.publish_ready(SETTINGS)
            self.assertFalse((Path(directory) / vpn.READINESS_ERROR).exists())


