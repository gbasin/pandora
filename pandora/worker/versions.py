"""The versions manifest: what a worker is supposed to be made of.

One file, read on the control machine, written to the worker, and compared
against what is actually installed every time anybody asks. Drift is reported,
never silently corrected: a worker that has drifted is a worker whose last
canary result is about a different machine.

The digest covers the *declaration* only. `generated` and `host` are facts
about one provisioning run and change on every call, so including them would
make two identical workers look different.

    [packages]                  # apt packages; the value is an exact dpkg
    incus = "6.0.5-8"           # version, or "*" for "any, but present"
    btrfs-progs = "*"

    [worker]
    root = "~/pandora"          # directory layout root
    engine_root = "~/pandora-engine"
    pool = "pandorapool"
    device = ""                 # a real block device; empty means loop file
    loop_size_gib = 32
    disk_floor_gib = 4          # admission stops below this much pool free
    max_running = 0             # concurrent runs; 0 means max(2, threads // 2)
    golden_keep = 2             # goldens kept per toolchain family by `worker gc`
"""
import hashlib
import json
import re
import tomllib
from pathlib import Path

from ..errors import ConfigError

# Docker is deliberately absent. It runs *inside* a golden, on the run's own
# nested dockerd, and a dockerd on the host would be a second trust domain with
# a second image store on the same disk.
PACKAGES = {
    'incus': '*',
    'incus-client': '*',
    'btrfs-progs': '*',
    'git': '*',
    'rsync': '*',
    'python3': '*',
}

WORKER = {
    'root': '~/pandora',
    'engine_root': '~/pandora-engine',
    'project': 'pandora',
    'pool': 'pandorapool',
    'profile': 'runner',
    'bridge': 'pandorabr0',
    'subnet': '10.141.0.1/24',
    'device': '',
    'loop_size_gib': 32,
    'disk_floor_gib': 4,
    'run_disk_gib': 12,
    'golden_keep': 2,
    'max_running': 0,           # 0 derives the run cap from the host's threads
    'unattended_upgrades': False,
    'user': 'ubuntu',
}

INTS = ('loop_size_gib', 'disk_floor_gib', 'run_disk_gib', 'golden_keep', 'max_running')

# A shared worker's named users, declared beside the machine they can reach:
#
#     [[users]]
#     name = "sterling"              # the client name this key speaks as
#     role = "user"                  # or "admin": a plain shell, no gateway
#     key = "ssh-ed25519 AAAA..."    # one authorized_keys entry
#
# `provision` renders them as the managed block of the worker user's
# `authorized_keys`: a user gets `restrict` plus the gateway forced command,
# an admin a plain line. Removing an entry and re-running revokes it.
USER_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}')
USER_KEY = re.compile(
    r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|'
    r'sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)'
    r' [A-Za-z0-9+/=]+( .*)?\Z')
USER_ROLES = ('user', 'admin')


def _users(raw):
    """Validate the `[[users]]` entries of a parsed versions.toml."""
    users = []
    for index, item in enumerate(raw.get('users') or []):
        where = 'users[%d]' % index
        if not isinstance(item, dict):
            raise ConfigError('%s must be a table with name, key and optional role' % where)
        strange = sorted(set(item) - {'name', 'key', 'role'})
        if strange:
            raise ConfigError('%s has unknown key%s %s; allowed: name, key, role'
                              % (where, '' if len(strange) == 1 else 's',
                                 ', '.join(strange)))
        name, key = item.get('name'), item.get('key')
        if not isinstance(name, str) or not USER_NAME.fullmatch(name):
            raise ConfigError('%s.name must be 1 to 64 letters, digits and . _ @ + -, '
                              'starting with a letter or digit' % where)
        if not isinstance(key, str) or not USER_KEY.fullmatch(key) or '\n' in key:
            raise ConfigError('%s.key must be one public key line '
                              '(ssh-ed25519, ecdsa, sk-*, or ssh-rsa)' % where)
        role = item.get('role', 'user')
        if role not in USER_ROLES:
            raise ConfigError('%s.role must be user or admin, not %r' % (where, role))
        users.append({'name': name, 'key': key.strip(), 'role': role})
    names = [user['name'] for user in users]
    if len(set(names)) != len(names):
        raise ConfigError('[[users]] names must be unique')
    return users


def normalize(raw):
    """Validate a parsed versions.toml and fill in the defaults."""
    unknown = sorted(set(raw) - {'packages', 'worker', 'users'})
    if unknown:
        raise ConfigError('versions.toml has unknown table%s %s; allowed: packages, '
                          'worker, users'
                          % ('' if len(unknown) == 1 else 's', ', '.join(unknown)))
    packages = dict(PACKAGES)
    for name, version in (raw.get('packages') or {}).items():
        if not isinstance(version, str):
            raise ConfigError('packages.%s must be a string version or "*"' % name)
        packages[name] = version
    worker = dict(WORKER)
    block = raw.get('worker') or {}
    allowed = set(WORKER) | {'min_engine_version'}
    strange = sorted(set(block) - allowed)
    if strange:
        raise ConfigError('[worker] has unknown key%s %s; allowed: %s'
                          % ('' if len(strange) == 1 else 's', ', '.join(strange),
                             ', '.join(sorted(allowed))))
    worker.update(block)
    for key in INTS:
        if not isinstance(worker[key], int) or isinstance(worker[key], bool):
            raise ConfigError('[worker] %s must be an integer' % key)
    floor = worker.get('min_engine_version')
    if floor is not None and (not isinstance(floor, int) or isinstance(floor, bool)):
        raise ConfigError('[worker] min_engine_version must be an integer')
    if worker['max_running'] < 0:
        raise ConfigError('[worker] max_running must be 0 (derive from threads) or a positive count')
    if not isinstance(worker['unattended_upgrades'], bool):
        raise ConfigError('[worker] unattended_upgrades must be true or false')
    if worker['device'] and not worker['device'].startswith('/dev/'):
        raise ConfigError('[worker] device must be a /dev path, not %r' % worker['device'])
    return {'packages': packages, 'worker': worker, 'users': _users(raw)}


def load(path=None):
    if path is None:
        return normalize({})
    path = Path(path).expanduser()
    if not path.is_file():
        raise ConfigError('no versions manifest at %s' % path)
    try:
        return normalize(tomllib.loads(path.read_text()))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError('%s is not valid TOML: %s' % (path, error)) from None


def digest(manifest):
    """The identity of a declaration, ignoring facts about one run of it."""
    body = {'packages': manifest['packages'], 'worker': manifest['worker'],
            'users': manifest.get('users') or []}
    return hashlib.sha256(json.dumps(body, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()[:16]


def drift(manifest, installed):
    """What the worker has that the manifest did not ask for, and vice versa.

    A wanted version of `*` is satisfied by any version, so `*` reports a
    package that is *missing* and never one that is merely a different build.
    Everything else is compared exactly, because "pinned" that tolerates a
    near-miss is not pinned.
    """
    items = []
    have = installed.get('packages') or {}
    for name, want in sorted(manifest['packages'].items()):
        got = have.get(name)
        if got is None:
            items.append({'kind': 'package', 'name': name, 'want': want, 'have': None,
                          'detail': 'not installed'})
        elif want != '*' and got != want:
            items.append({'kind': 'package', 'name': name, 'want': want, 'have': got,
                          'detail': 'version differs'})
    for key, want in sorted(manifest['worker'].items()):
        if key not in (installed.get('worker') or {}):
            continue
        got = installed['worker'][key]
        if str(got) != str(want):
            items.append({'kind': 'worker', 'name': key, 'want': want, 'have': got,
                          'detail': 'differs from the manifest'})
    for name, detail in sorted((installed.get('missing') or {}).items()):
        items.append({'kind': 'object', 'name': name, 'want': 'present', 'have': None,
                      'detail': detail})
    return items


def render(manifest):
    """The manifest as the TOML a person would have written."""
    lines = ['# Written by `pandora worker provision`. Edit and re-provision to change.',
             '', '[packages]']
    for name, version in sorted(manifest['packages'].items()):
        lines.append('%s = "%s"' % (name, version))
    lines += ['', '[worker]']
    for key, value in sorted(manifest['worker'].items()):
        if value is None:
            continue
        if isinstance(value, bool):
            lines.append('%s = %s' % (key, 'true' if value else 'false'))
        elif isinstance(value, int):
            lines.append('%s = %d' % (key, value))
        else:
            lines.append('%s = "%s"' % (key, value))
    for user in manifest.get('users') or []:
        lines += ['', '[[users]]',
                  'name = "%s"' % user['name'],
                  'role = "%s"' % user['role'],
                  'key = "%s"' % user['key']]
    return '\n'.join(lines) + '\n'
