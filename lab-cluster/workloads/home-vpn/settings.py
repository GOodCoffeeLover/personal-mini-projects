#!/usr/bin/env python3
"""Export/edit/import home-vpn configuration. Python stdlib and kubectl only."""
import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
DEFAULT_KUBECONFIG = HERE.parents[1] / '.state' / 'lab-cluster.kubeconfig'
FILES = {
    'home-vpn-settings': ('settings.json', 'direct-domains.json', 'TS_HOSTNAME', 'TS_EXTRA_ARGS'),
    'home-vpn-upstream': ('outbound.json',),
}


class SettingsError(Exception):
    """An error safe to display without exposing configuration values."""


class Kubectl:
    def __init__(self, kubeconfig, namespace):
        self.base = ['kubectl', '--kubeconfig', str(kubeconfig), '--namespace', namespace]
        # API access must survive restarting the VPN used by the shell's proxy.
        self.env = {key: value for key, value in os.environ.items()
                    if key.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')}

    def run(self, *args, value=None, wait=False):
        command = self.base + ([] if wait else ['--request-timeout=30s']) + list(args)
        try:
            result = subprocess.run(command, input=value, text=True, capture_output=True,
                                    env=self.env, timeout=615 if wait else 45)
        except FileNotFoundError:
            raise SettingsError('kubectl was not found in PATH.') from None
        except subprocess.TimeoutExpired:
            raise SettingsError(f'kubectl {args[0]} timed out.') from None
        except OSError:
            raise SettingsError('Cannot execute kubectl.') from None
        if result.returncode:
            # API errors may echo Secret data or credentials. Do not print them.
            raise SettingsError(f'kubectl {args[0]} failed (exit {result.returncode}); '
                                'check API access, permissions and resource status.')
        return result.stdout


def parse_response(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        raise SettingsError('kubectl returned an invalid JSON response.') from None


def read_secrets(client, required):
    args = ['get', 'secret', *FILES, '-o', 'json']
    if not required:
        args.append('--ignore-not-found')
    response = parse_response(client.run(*args))
    if not isinstance(response, dict) or response.get('kind') != 'List':
        raise SettingsError('Expected a Secret list from kubectl.')
    items = response.get('items')
    if not isinstance(items, list):
        raise SettingsError('Expected a Secret list from kubectl.')
    secrets = {}
    for item in items:
        if not isinstance(item, dict) or item.get('kind') != 'Secret':
            raise SettingsError('Unexpected resource in kubectl response.')
        name = item.get('metadata', {}).get('name')
        if name not in FILES or name in secrets or item.get('type', 'Opaque') != 'Opaque':
            raise SettingsError('Unexpected Secret name, duplicate or non-Opaque Secret.')
        secrets[name] = item
    if required and set(secrets) != set(FILES):
        raise SettingsError('Both configuration Secrets must exist before export.')
    return secrets


def export_settings(client, directory):
    secrets = read_secrets(client, required=True)
    decoded = {}
    for name, keys in FILES.items():
        data = secrets[name].get('data', {})
        for key in keys:
            try:
                decoded[key] = base64.b64decode(data[key], validate=True)
                decoded[key].decode('utf-8')
            except (KeyError, TypeError, ValueError, binascii.Error):
                raise SettingsError(f'{name}: missing or invalid UTF-8/base64 value for {key}.') from None
    # Fetch and decode all values before touching the previous local export.
    if directory.is_symlink():
        raise SettingsError('The export directory must not be a symlink.')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix='.export-', dir=directory) as temporary:
        staging = Path(temporary)
        for key, value in decoded.items():
            with open(staging / key, 'xb', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
                stream.write(value)
        for key in decoded:
            os.replace(staging / key, directory / key)
    print(f'Exported {len(decoded)} configuration files to {directory}.')


def reject_constant(_):
    raise ValueError('Non-standard JSON constant')


def read_files(directory):
    files = {}
    for keys in FILES.values():
        for key in keys:
            try:
                value = (directory / key).read_bytes()
                text = value.decode('utf-8')
            except (OSError, UnicodeError):
                raise SettingsError(f'Cannot read UTF-8 configuration file: {key}.') from None
            if key.endswith('.json'):
                try:
                    parsed = json.loads(text, parse_constant=reject_constant)
                except ValueError:
                    raise SettingsError(f'Invalid JSON in {key}.') from None
                expected = list if key == 'direct-domains.json' else dict
                if not isinstance(parsed, expected):
                    raise SettingsError(f'{key} must contain a JSON {expected.__name__}.')
                if key == 'direct-domains.json' and not all(isinstance(x, str) and x.strip() for x in parsed):
                    raise SettingsError('direct-domains.json must contain nonempty strings.')
            elif not text.strip() or '\x00' in text:
                raise SettingsError(f'{key} must contain nonempty text without NUL bytes.')
            files[key] = base64.b64encode(value).decode('ascii')
    return files


def import_settings(client, directory, namespace):
    files = read_files(directory)
    secrets = read_secrets(client, required=False)
    deployment_text = client.run('get', 'deployment/home-vpn', '--ignore-not-found', '-o', 'json')
    deployment = parse_response(deployment_text) if deployment_text.strip() else None
    if deployment is not None:
        if not isinstance(deployment, dict) or deployment.get('kind') != 'Deployment':
            raise SettingsError('Expected the home-vpn Deployment from kubectl.')
        if deployment.get('spec', {}).get('paused'):
            raise SettingsError('Deployment home-vpn is paused; resume it before importing settings.')
    # Validate all update preconditions before the first write.
    for name, secret in secrets.items():
        if not secret.get('metadata', {}).get('resourceVersion'):
            raise SettingsError(f'{name}: resourceVersion is missing from kubectl response.')
    changed = []
    try:
        for name, keys in FILES.items():
            data = {key: files[key] for key in keys}
            if name in secrets:
                patch = {'metadata': {'resourceVersion': secrets[name]['metadata']['resourceVersion']}, 'data': data}
                client.run('patch', 'secret', name, '--type=merge', '--patch-file=/dev/stdin',
                           value=json.dumps(patch))
            else:
                secret = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
                          'metadata': {'name': name, 'namespace': namespace}, 'data': data}
                client.run('create', '-f', '-', value=json.dumps(secret))
            changed.append(name)
            print(f'Updated {name}.', flush=True)
    except SettingsError as error:
        updated = ', '.join(changed) or 'none'
        raise SettingsError(f'{error} Secrets already updated: {updated}. '
                            'Deployment restart was not requested.') from None
    if deployment is None:
        print('Deployment home-vpn does not exist; configuration Secrets are ready.')
        return
    try:
        client.run('rollout', 'restart', 'deployment/home-vpn')
        if deployment.get('spec', {}).get('replicas', 1) == 0:
            print('Deployment template restarted; replicas remain at zero.')
        else:
            print('Waiting for Deployment home-vpn rollout (up to 10 minutes)...', flush=True)
            client.run('rollout', 'status', 'deployment/home-vpn', '--timeout=600s', wait=True)
            print('Deployment home-vpn rollout completed.')
    except SettingsError as error:
        raise SettingsError(f'Both configuration Secrets were updated. {error} '
                            'Check Deployment home-vpn; no automatic rollback was performed.') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('export', 'import'):
        child = commands.add_parser(command)
        child.add_argument('--kubeconfig', type=Path, default=DEFAULT_KUBECONFIG)
        child.add_argument('--namespace', default='home-vpn')
        child.add_argument('--directory', type=Path, default=HERE / 'settings.local')
    args = parser.parse_args(argv)
    client = Kubectl(args.kubeconfig.expanduser(), args.namespace)
    try:
        if args.command == 'export':
            export_settings(client, args.directory.expanduser())
        else:
            import_settings(client, args.directory.expanduser(), args.namespace)
    except SettingsError as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    except OSError:
        print('Error: cannot access local configuration files or execute kubectl.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
