"""Offline checks. Requires kubectl (Kustomize) and Ruby's standard YAML library."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[2]


def yaml_documents(value):
    return json.loads(subprocess.check_output([
        'ruby', '-rjson', '-ryaml', '-e', 'puts JSON.generate(YAML.load_stream(STDIN.read))',
    ], input=value, text=True))


def build(directory):
    return yaml_documents(subprocess.check_output(['kubectl', 'kustomize', str(directory)], text=True))


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = build(HERE)
        cls.by_kind = {item['kind']: item for item in cls.items}
        cls.pod = cls.by_kind['Deployment']['spec']['template']['spec']

    def test_single_isolated_deployment(self):
        spec = self.by_kind['Deployment']['spec']
        self.assertEqual(spec['replicas'], 1)
        self.assertEqual(spec['strategy'], {'type': 'Recreate'})
        for key in ('hostNetwork', 'hostPID', 'hostIPC', 'automountServiceAccountToken'):
            self.assertFalse(self.pod[key])
        self.assertEqual(self.pod['nodeSelector'], {'kubernetes.io/os': 'linux', 'kubernetes.io/arch': 'amd64'})
        self.assertNotIn('hostAliases', self.pod)
        self.assertTrue(all('hostPath' not in v for v in self.pod['volumes']))
        self.assertEqual(set(self.by_kind), {'Namespace', 'ServiceAccount', 'Role', 'RoleBinding', 'ConfigMap', 'Deployment'})

    def test_scoped_credentials_and_container_permissions(self):
        init = self.pod['initContainers']
        self.assertEqual([c['name'] for c in init], ['network-init', 'tailscale', 'xray-configure', 'xray-validate'])
        self.assertEqual(init[1]['restartPolicy'], 'Always')
        self.assertIn('startupProbe', init[1])
        all_containers = init + self.pod['containers']
        users = lambda name: [c['name'] for c in all_containers if any(m['name'] == name for m in c['volumeMounts'])]
        self.assertEqual(users('api-token'), ['tailscale'])
        self.assertEqual(users('upstream'), ['xray-configure'])
        self.assertEqual(users('settings'), ['network-init', 'xray-configure', 'network-guard'])
        self.assertEqual([c['name'] for c in all_containers if c['securityContext'].get('privileged')],
                         ['network-init', 'tailscale'])
        self.assertEqual([c['name'] for c in all_containers if 'livenessProbe' in c], ['network-guard'])
        for name in ('xray', 'network-guard', 'xray-validate'):
            c = next(c for c in all_containers if c['name'] == name)
            security = c['securityContext']
            self.assertFalse(security['allowPrivilegeEscalation'])
            self.assertTrue(security['readOnlyRootFilesystem'])
            self.assertEqual(security['capabilities'], {'drop': ['ALL'], 'add': ['NET_ADMIN']})
        images = {c['image'] for c in all_containers}
        self.assertEqual(images, {
            'docker.io/tailscale/tailscale:v1.98.4@sha256:25cde9ad76020b0e29229136d0c38b5962e9a0e1774ffac9b0df68e4a37d6cf0',
            'docker.io/nicolaka/netshoot:v0.14@sha256:7f08c4aff13ff61a35d30e30c5c1ea8396eac6ab4ce19fd02d5a4b3b5d0d09a2',
            'ghcr.io/xtls/xray-core:26.3.23@sha256:581394b25833b419c69b39e1e7f07e03b6cb2fb957cfe5b831f25aba986bd99e',
        })

    def test_only_external_secrets_and_no_enrollment_key(self):
        volumes = {v['name']: v for v in self.pod['volumes']}
        self.assertEqual(volumes['settings']['secret']['secretName'], 'home-vpn-settings')
        self.assertEqual(volumes['upstream']['secret']['secretName'], 'home-vpn-upstream')
        self.assertEqual({item['key'] for item in volumes['settings']['secret']['items']},
                         {'settings.json', 'direct-domains.json'})
        env = {entry['name']: entry for entry in self.pod['initContainers'][1]['env']}
        self.assertEqual(env['TS_KUBE_SECRET']['value'], 'home-vpn-state')
        self.assertEqual(env['TS_KUBERNETES_READ_API_SERVER_ADDRESS_FROM_ENV']['value'], 'true')
        self.assertEqual(env['TS_AUTH_ONCE']['value'], 'true')
        self.assertNotIn('TS_AUTHKEY', env)
        for key in ('TS_HOSTNAME', 'TS_EXTRA_ARGS'):
            self.assertEqual(env[key]['valueFrom']['secretKeyRef'], {'name': 'home-vpn-settings', 'key': key})
        self.assertEqual(self.by_kind['Role']['rules'], [
            {'apiGroups': [''], 'resources': ['secrets'], 'resourceNames': ['home-vpn-state'],
             'verbs': ['get', 'update', 'patch']},
            {'apiGroups': [''], 'resources': ['events'], 'verbs': ['get', 'create', 'patch']},
        ])
        self.assertFalse(self.by_kind['ServiceAccount']['automountServiceAccountToken'])
        self.assertEqual(self.by_kind['RoleBinding']['subjects'],
                         [{'kind': 'ServiceAccount', 'name': 'home-vpn', 'namespace': 'home-vpn'}])

    def test_code_only_configmap_and_rollout_hash(self):
        cm = self.by_kind['ConfigMap']
        self.assertEqual(cm['data'], {'runtime.py': (HERE / 'runtime.py').read_text()})
        name = cm['metadata']['name']
        volume = next(v for v in self.pod['volumes'] if v['name'] == 'config')
        self.assertEqual(volume['configMap']['name'], name)
        self.assertNotEqual(name, 'home-vpn-config')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'home-vpn'
            shutil.copytree(HERE, target, ignore=shutil.ignore_patterns('settings.local', '__pycache__'))
            private = target / 'settings.local'
            private.mkdir()
            (private / 'synthetic.yaml').write_text('apiVersion: v1\nkind: Secret\nmetadata:\n  name: synthetic\n')
            (target / 'runtime.py').write_text((target / 'runtime.py').read_text() + '\n# rollout test\n')
            items = build(target)
            self.assertEqual(len(items), 6)
            changed = {item['kind']: item for item in items}
            self.assertNotIn('Secret', changed)
            self.assertNotEqual(changed['ConfigMap']['metadata']['name'], name)
            volumes = changed['Deployment']['spec']['template']['spec']['volumes']
            self.assertEqual(next(v['configMap']['name'] for v in volumes if v['name'] == 'config'),
                             changed['ConfigMap']['metadata']['name'])

    def test_manual_application(self):
        app = yaml_documents((REPO / 'lab-cluster/applications/home-vpn.yaml').read_text())[0]
        self.assertNotIn('finalizers', app['metadata'])
        self.assertNotIn('automated', app['spec']['syncPolicy'])
        self.assertEqual(set(app['spec']['syncPolicy']['syncOptions']), {'Prune=false', 'ServerSideApply=true'})
        self.assertEqual(app['spec']['source']['path'], 'lab-cluster/workloads/home-vpn')
        self.assertEqual(app['spec']['destination']['namespace'], 'home-vpn')

    def test_private_state_is_ignored(self):
        subprocess.run(['git', 'check-ignore', '-q', '.state/home-vpn/settings.json'], cwd=REPO, check=True)
        subprocess.run(['git', 'check-ignore', '-q',
                        'lab-cluster/workloads/home-vpn/settings.local/outbound.json'], cwd=REPO, check=True)
