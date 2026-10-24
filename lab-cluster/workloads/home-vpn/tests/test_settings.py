"""Offline configuration round trips; no live cluster or local credentials."""
import base64
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import settings as tool


VALUES = {
    'settings.json': b'{"tcp_retries2":8}',
    'direct-domains.json': '["domain:example.com", "keyword:пример"]\n'.encode(),
    'TS_HOSTNAME': b'synthetic-home-vpn\n',
    'TS_EXTRA_ARGS': b'--login-server=https://control.example.com --accept-routes=false --advertise-exit-node=false\n',
    'outbound.json': b'{"tag":"upstream","protocol":"vless"}\n',
}


def secret(name):
    return {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
            'metadata': {'name': name, 'namespace': 'test', 'resourceVersion': '10',
                         'labels': {'keep': 'yes'}, 'annotations': {'keep': 'yes'}},
            'data': {**{key: base64.b64encode(VALUES[key]).decode() for key in tool.FILES[name]},
                     'extra-key': 'ZXh0cmE='}}


class FakeKubectl:
    def __init__(self, replicas=1, present=None, deployment=True, fail=None):
        self.secrets = {name: secret(name) for name in (tool.FILES if present is None else present)}
        self.deployment = ({'kind': 'Deployment', 'spec': {'replicas': replicas}} if deployment else None)
        self.calls = []
        self.fail = fail

    def run(self, *args, value=None, wait=False):
        self.calls.append((args, value, wait))
        if self.fail and args[:len(self.fail)] == self.fail:
            raise tool.SettingsError('Synthetic API error.')
        if args[:2] == ('get', 'secret'):
            names = args[2:4]
            if '--ignore-not-found' not in args and any(name not in self.secrets for name in names):
                raise tool.SettingsError('Missing configuration Secret.')
            return json.dumps({'kind': 'List', 'items': [self.secrets[name] for name in names if name in self.secrets]})
        if args[:2] == ('get', 'deployment/home-vpn'):
            return json.dumps(self.deployment) if self.deployment else ''
        if args[0] == 'patch':
            name = args[2]
            payload = json.loads(value)
            assert payload['metadata']['resourceVersion'] == self.secrets[name]['metadata']['resourceVersion']
            self.secrets[name]['data'].update(payload['data'])
            return 'patched'
        if args[0] == 'create':
            payload = json.loads(value)
            self.secrets[payload['metadata']['name']] = payload
            return 'created'
        if args[0] == 'rollout':
            return 'done'
        raise AssertionError(f'Unexpected command: {args}')


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'settings.local'
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def local_files(self):
        self.directory.mkdir(exist_ok=True)
        for name, value in VALUES.items():
            (self.directory / name).write_bytes(value)

    def test_export_decodes_only_configuration_and_sets_private_permissions(self):
        client = FakeKubectl()
        # The fake server contains identity state, but requests must name only configuration Secrets.
        client.secrets['home-vpn-state'] = {'data': {'_machinekey': 'ZG8tbm90LXJlYWQ='}}
        tool.export_settings(client, self.directory)
        self.assertEqual({p.name for p in self.directory.iterdir()}, set(VALUES))
        for name, value in VALUES.items():
            self.assertEqual((self.directory / name).read_bytes(), value)
            self.assertEqual(stat.S_IMODE((self.directory / name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(client.calls[0][0][2:4], tuple(tool.FILES))
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn('control.example.com', self.output.getvalue())

    def test_export_api_or_decoding_errors_preserve_previous_files(self):
        self.local_files()
        cases = [FakeKubectl(fail=('get',)), FakeKubectl(present=['home-vpn-settings'])]
        for bad in (None, '!invalid!', base64.b64encode(b'\xff').decode()):
            client = FakeKubectl()
            if bad is None:
                del client.secrets['home-vpn-upstream']['data']['outbound.json']
            else:
                client.secrets['home-vpn-upstream']['data']['outbound.json'] = bad
            cases.append(client)
        for client in cases:
            with self.subTest(client=client), self.assertRaises(tool.SettingsError):
                tool.export_settings(client, self.directory)
            self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir()}, VALUES)

    def test_export_atomically_replaces_previous_files(self):
        self.local_files()
        for file in self.directory.iterdir():
            file.write_bytes(b'old')
        tool.export_settings(FakeKubectl(), self.directory)
        self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir()}, VALUES)

    def test_export_import_round_trip_preserves_unknown_keys_and_metadata(self):
        client = FakeKubectl()
        state = {'data': {'_machinekey': 'ZG8tbm90LXRvdWNo'}}
        client.secrets['home-vpn-state'] = copy.deepcopy(state)
        metadata = copy.deepcopy(client.secrets['home-vpn-settings']['metadata'])
        tool.export_settings(client, self.directory)
        (self.directory / 'settings.json').write_text('{"tcp_retries2":9}\n')
        tool.import_settings(client, self.directory, 'test')
        updated = client.secrets['home-vpn-settings']
        self.assertEqual(updated['metadata'], metadata)
        self.assertEqual(updated['data']['extra-key'], 'ZXh0cmE=')
        self.assertEqual(base64.b64decode(updated['data']['settings.json']), b'{"tcp_retries2":9}\n')
        self.assertEqual(client.secrets['home-vpn-state'], state)
        mutations = [c for c in client.calls if c[0][0] != 'get']
        self.assertEqual([c[0][:3] for c in mutations], [
            ('patch', 'secret', 'home-vpn-settings'), ('patch', 'secret', 'home-vpn-upstream'),
            ('rollout', 'restart', 'deployment/home-vpn'), ('rollout', 'status', 'deployment/home-vpn')])
        self.assertTrue(mutations[-1][2])
        for args, payload, _ in mutations[:2]:
            self.assertIn('--patch-file=/dev/stdin', args)
            self.assertEqual(set(json.loads(payload)), {'metadata', 'data'})
            self.assertNotIn('control.example.com', ' '.join(args))
        self.assertNotIn('control.example.com', self.output.getvalue())

    def test_import_creates_missing_secrets_without_creating_deployment(self):
        self.local_files()
        client = FakeKubectl(present=[], deployment=False)
        tool.import_settings(client, self.directory, 'custom')
        self.assertEqual(set(client.secrets), set(tool.FILES))
        for name, keys in tool.FILES.items():
            obj = client.secrets[name]
            self.assertEqual(obj['metadata'], {'name': name, 'namespace': 'custom'})
            self.assertEqual(obj['type'], 'Opaque')
            self.assertEqual(obj['data'], {key: base64.b64encode(VALUES[key]).decode() for key in keys})
        self.assertFalse(any(c[0][0] == 'rollout' for c in client.calls))

    def test_import_does_not_start_a_scaled_down_deployment(self):
        self.local_files()
        client = FakeKubectl(replicas=0)
        tool.import_settings(client, self.directory, 'test')
        rollouts = [c[0] for c in client.calls if c[0][0] == 'rollout']
        self.assertEqual(rollouts, [('rollout', 'restart', 'deployment/home-vpn')])
        self.assertEqual(client.deployment['spec']['replicas'], 0)
        self.assertFalse(any(c[0][0] == 'scale' for c in client.calls))

    def test_invalid_or_missing_local_files_fail_before_any_api_request(self):
        cases = [('outbound.json', None), ('outbound.json', b'{"secret":"SENSITIVE_VALUE"'),
                 ('settings.json', b'NaN'), ('settings.json', b'[]'),
                 ('direct-domains.json', b'{}'), ('direct-domains.json', b'[null]'),
                 ('TS_HOSTNAME', b''), ('TS_EXTRA_ARGS', b'bad\x00value')]
        for name, value in cases:
            self.local_files()
            if value is None:
                (self.directory / name).unlink()
            else:
                (self.directory / name).write_bytes(value)
            client = FakeKubectl()
            with self.subTest(name=name, value=value), self.assertRaises(tool.SettingsError) as error:
                tool.import_settings(client, self.directory, 'test')
            self.assertNotIn('SENSITIVE_VALUE', str(error.exception))
            self.assertEqual(client.calls, [])

    def test_preflight_errors_do_not_change_secrets(self):
        self.local_files()
        clients = [FakeKubectl(fail=('get', 'secret')), FakeKubectl(fail=('get', 'deployment/home-vpn'))]
        paused = FakeKubectl()
        paused.deployment['spec']['paused'] = True
        clients.append(paused)
        missing_version = FakeKubectl()
        del missing_version.secrets['home-vpn-upstream']['metadata']['resourceVersion']
        clients.append(missing_version)
        for client in clients:
            before = copy.deepcopy(client.secrets)
            with self.subTest(client=client), self.assertRaises(tool.SettingsError):
                tool.import_settings(client, self.directory, 'test')
            self.assertEqual(client.secrets, before)
            self.assertTrue(all(c[0][0] == 'get' for c in client.calls))

    def test_second_update_failure_reports_partial_success_without_restart(self):
        self.local_files()
        client = FakeKubectl(fail=('patch', 'secret', 'home-vpn-upstream'))
        (self.directory / 'settings.json').write_text('{"changed":true}')
        with self.assertRaises(tool.SettingsError) as error:
            tool.import_settings(client, self.directory, 'test')
        self.assertIn('Secrets already updated: home-vpn-settings.', str(error.exception))
        self.assertIn('restart was not requested', str(error.exception))
        self.assertEqual(base64.b64decode(client.secrets['home-vpn-settings']['data']['settings.json']), b'{"changed":true}')
        self.assertFalse(any(c[0][0] == 'rollout' for c in client.calls))

    def test_create_failure_does_not_restart(self):
        self.local_files()
        client = FakeKubectl(present=['home-vpn-settings'], fail=('create',))
        with self.assertRaisesRegex(tool.SettingsError, 'Secrets already updated: home-vpn-settings'):
            tool.import_settings(client, self.directory, 'test')
        self.assertNotIn('home-vpn-upstream', client.secrets)
        self.assertFalse(any(c[0][0] == 'rollout' for c in client.calls))

    def test_rollout_failure_reports_that_both_secrets_were_updated(self):
        self.local_files()
        for action in ('restart', 'status'):
            client = FakeKubectl(fail=('rollout', action))
            with self.subTest(action=action), self.assertRaisesRegex(tool.SettingsError, 'Both configuration Secrets were updated'):
                tool.import_settings(client, self.directory, 'test')
            self.assertEqual(sum(c[0][0] == 'patch' for c in client.calls), 2)


class CommandTest(unittest.TestCase):
    def test_defaults_are_independent_of_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                with patch.object(tool, 'Kubectl') as constructor, patch.object(tool, 'export_settings') as export:
                    self.assertEqual(tool.main(['export']), 0)
                    constructor.assert_called_once_with(tool.HERE.parents[1] / '.state/lab-cluster.kubeconfig', 'home-vpn')
                    export.assert_called_once_with(constructor.return_value, tool.HERE / 'settings.local')
            finally:
                os.chdir(previous)

    def test_explicit_options_and_cli_error_exit(self):
        with patch.object(tool, 'Kubectl') as constructor, patch.object(tool, 'import_settings') as load:
            load.side_effect = tool.SettingsError('Synthetic failure.')
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                code = tool.main(['import', '--namespace', 'custom', '--kubeconfig', '/tmp/synthetic-kubeconfig',
                                  '--directory', '/tmp/synthetic-settings'])
            self.assertEqual(code, 1)
            self.assertIn('Synthetic failure', stderr.getvalue())
            constructor.assert_called_once_with(Path('/tmp/synthetic-kubeconfig'), 'custom')
            load.assert_called_once_with(constructor.return_value, Path('/tmp/synthetic-settings'), 'custom')

    def test_kubectl_removes_proxy_environment_and_uses_stdin_for_payload(self):
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://vpn.invalid', 'https_proxy': 'http://vpn.invalid',
                                    'ALL_PROXY': 'socks5://vpn.invalid', 'NO_PROXY': 'localhost'}):
            client = tool.Kubectl(Path('/tmp/synthetic-kubeconfig'), 'custom')
        with patch.object(tool.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'ok', '')) as run:
            self.assertEqual(client.run('create', '-f', '-', value='SENSITIVE_VALUE'), 'ok')
            call = run.call_args
            self.assertFalse(any(k.lower() in ('http_proxy', 'https_proxy', 'all_proxy') for k in call.kwargs['env']))
            self.assertEqual(call.kwargs['env']['NO_PROXY'], 'localhost')
            self.assertEqual(call.kwargs['input'], 'SENSITIVE_VALUE')
            self.assertNotIn('SENSITIVE_VALUE', ' '.join(call.args[0]))
            self.assertEqual(call.args[0][1:5], ['--kubeconfig', '/tmp/synthetic-kubeconfig', '--namespace', 'custom'])

    def test_kubectl_errors_never_echo_secret_values(self):
        client = tool.Kubectl(Path('/tmp/synthetic-kubeconfig'), 'test')
        failure = subprocess.CompletedProcess([], 1, 'SENSITIVE_VALUE', 'SENSITIVE_VALUE')
        with patch.object(tool.subprocess, 'run', return_value=failure), self.assertRaises(tool.SettingsError) as error:
            client.run('patch', 'secret', 'home-vpn-settings')
        self.assertNotIn('SENSITIVE_VALUE', str(error.exception))
        for failure in (subprocess.TimeoutExpired('kubectl', 45, output='SENSITIVE_VALUE'), FileNotFoundError()):
            with patch.object(tool.subprocess, 'run', side_effect=failure), self.assertRaises(tool.SettingsError) as error:
                client.run('get', 'secret', *tool.FILES)
            self.assertNotIn('SENSITIVE_VALUE', str(error.exception))


if __name__ == '__main__':
    unittest.main()
