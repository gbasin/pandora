"""What the worker actually is, read from the worker itself.

`provision.sh` prints the same survey at the end of a provisioning run; this is
the one `pandora worker status` uses afterward, so drift is answered from the
live machine rather than from a file written when it was last touched.

Two surveys share one comparison (`drift`). `survey` is the whole one `status`
prints, `sudo incus` calls included. `quick_survey` is the part the engine can
afford before signing each verdict: the manifest's packages in one
`dpkg-query`, the settings the manifest pins, and the kernel.
"""
import os
import subprocess
from pathlib import Path

from . import versions


class Unreadable(Exception):
    """A fact the drift check needs could not be read. The message says which."""


def sh(command, timeout=60):
    proc = subprocess.run(['sh', '-c', command], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=timeout)
    return (proc.stdout or b'').decode('utf-8', 'replace').strip()


# Package, architecture-qualified name, dpkg's three-letter status, version.
DPKG_FORMAT = '${Package}\t${binary:Package}\t${db:Status-Abbrev}\t${Version}\n'
# The quick survey runs before every signature, so each tool it calls gets a
# short leash. A tool that runs out of it reads as unreadable, which is drift.
QUICK_TIMEOUT = 5


def installed(status):
    """`ii`, or `hi` for a held package: wanted installed, installed, no error."""
    return len(status) >= 2 and status[1] == 'i' and status[2:].strip() == ''


def packages(names, timeout=10):
    """`(versions, notes)` for `names`, from one `dpkg-query` call.

    `versions` maps every name to its installed version, or None when dpkg
    knows no installed copy. A package dpkg holds in any state other than
    installed (half-installed, unpacked, config files only) is None too, and
    `notes` says which state, so a package mid-`apt` reads as drift rather
    than as present. Raises Unreadable when dpkg cannot be asked at all.
    """
    names = list(names)
    found, notes = {}, {}
    if names:
        try:
            proc = subprocess.run(['dpkg-query', '-W', '-f=' + DPKG_FORMAT, *names],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  timeout=timeout)
        except FileNotFoundError:
            raise Unreadable('dpkg-query not found') from None
        except subprocess.TimeoutExpired:
            raise Unreadable('dpkg-query timed out after %gs' % timeout) from None
        except OSError as error:
            raise Unreadable('dpkg-query: %s' % (error.strerror or error)) from None
        # 1 is "some name matched no package", which is an answer, not a failure.
        if proc.returncode not in (0, 1):
            raise Unreadable('dpkg-query exit %d' % proc.returncode)
        for line in proc.stdout.decode('utf-8', 'replace').splitlines():
            parts = line.split('\t')
            if len(parts) != 4:
                continue
            package, qualified, status, version = parts
            for key in (package, qualified):
                # Multi-arch: `foo:i386 rc` may precede `foo:amd64 ii`. For the
                # bare name an installed copy wins over one that is not.
                held = found.get(key)
                if held is None or (installed(status) and not installed(held[0])):
                    found[key] = (status, version)
    out = {}
    for name in names:
        status, version = found.get(name, ('', ''))
        if version and installed(status):
            out[name] = version
        else:
            out[name] = None
            if status:
                notes[name] = 'dpkg status %s' % status.strip()
    return out, notes


def package_versions(names):
    return packages(names)[0]


def settings(timeout=60):
    """The manifest's `[worker]` settings that can be read off the host.

    A setting `systemctl` cannot answer (missing, timed out, no state printed)
    is None, never False: None matches no manifest value, so it reads as
    drift. A unit that is not installed is an answer: False. Older systemd
    says so only on stderr.
    """
    try:
        proc = subprocess.run(['systemctl', 'is-enabled', 'unattended-upgrades'],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return {'unattended_upgrades': None}
    answer = (proc.stdout or b'').decode('utf-8', 'replace').strip()
    if not answer:
        err = (proc.stderr or b'').decode('utf-8', 'replace')
        if 'No such file' in err or 'not found' in err or 'not-found' in err:
            answer = 'not-found'
    return {'unattended_upgrades': (answer == 'enabled') if answer else None}


def kernel():
    """The running kernel release, as `uname -r` prints it; '' when unreadable."""
    try:
        return Path('/proc/sys/kernel/osrelease').read_text().strip()
    except OSError:
        return sh('uname -r')


def manifest_path(root):
    """Where provisioning stored the worker's manifest."""
    return Path(root).expanduser() / 'worker' / 'versions.toml'


def drift(manifest, observed, state):
    """Every way the host differs from what it was made to be.

    The one comparison: `pandora worker status` and the engine's signing check
    both call it. `versions.drift` covers packages, settings and missing
    objects; the kernel is not something the manifest pins, but a kernel other
    than the one the last canary passed on is a different machine, so it is
    drift too.
    """
    items = versions.drift(manifest, observed)
    current = (observed.get('host') or {}).get('kernel', '')
    if (state or {}).get('kernel') and state['kernel'] != current:
        items.append({'kind': 'host', 'name': 'kernel', 'want': state['kernel'],
                      'have': current, 'detail': 'the canary passed on a different kernel'})
    return items


def quick_survey(manifest):
    """The part of `survey` cheap enough to run before signing: packages,
    settings and the kernel, without the `sudo incus` object checks.
    Each tool gets QUICK_TIMEOUT seconds. Raises Unreadable when dpkg cannot
    be asked."""
    found, notes = packages(sorted(manifest['packages']), timeout=QUICK_TIMEOUT)
    return {'packages': found, 'package_notes': notes,
            'worker': settings(timeout=QUICK_TIMEOUT),
            'host': {'kernel': kernel()}}


def survey(manifest):
    """`{packages, worker, missing, host}` in the shape `versions.drift` wants.

    `missing` holds named objects rather than versions -- a pool that is gone
    is not a package at the wrong version, and reporting it as one would put it
    under a heading nobody reads.
    """
    worker = manifest['worker']
    project, pool, bridge = worker['project'], worker['pool'], worker['bridge']
    root = Path(worker['root']).expanduser()
    missing = {}
    if sh('sudo incus storage show %s >/dev/null 2>&1 && echo yes' % pool) != 'yes':
        missing['pool ' + pool] = 'no such storage pool'
    if sh('sudo incus project show %s >/dev/null 2>&1 && echo yes' % project) != 'yes':
        missing['project ' + project] = 'no such project'
    if sh('sudo incus network show %s >/dev/null 2>&1 && echo yes' % bridge) != 'yes':
        missing['bridge ' + bridge] = 'no such network'
    for unit in ('pandora-pool.service', 'pandora-net.service'):
        if worker['device'] and unit == 'pandora-pool.service':
            continue                      # a real device needs no loop unit
        if sh('systemctl is-enabled %s 2>/dev/null' % unit) != 'enabled':
            missing[unit] = 'not enabled'
    if sh('systemctl --user is-enabled pandora-engine.service 2>/dev/null') != 'enabled':
        missing['pandora-engine.service'] = 'not enabled (user unit)'
    if sh('loginctl show-user %s -p Linger --value 2>/dev/null' % worker['user']) != 'yes':
        missing['linger'] = 'not enabled for ' + worker['user']
    forward = sh('sudo iptables -S FORWARD | grep -c -- %s || true' % bridge)
    if forward.isdigit() and int(forward) < 2:
        missing['forward rules'] = 'only %s ACCEPT rule(s) for %s' % (forward, bridge)
    try:
        found, notes = packages(sorted(manifest['packages']))
    except Unreadable as error:
        # `status` still answers: every package reads as unknown, and why.
        found = {name: None for name in manifest['packages']}
        notes = {name: str(error) for name in manifest['packages']}
    return {
        'packages': found,
        'package_notes': notes,
        'worker': settings(),
        'missing': missing,
        'host': {'hostname': sh('hostname'), 'kernel': kernel(),
                 'cores': os.cpu_count() or 0,
                 'incus': sh('incus --version 2>/dev/null'),
                 'memory_mib': memory_mib(),
                 'root': str(root), 'root_free_gib': free_gib('/'),
                 'boot_id': sh('cat /proc/sys/kernel/random/boot_id'),
                 'uptime_seconds': int(float(sh("awk '{print $1}' /proc/uptime") or 0)),
                 'booted': read(root / 'worker' / 'booted')},
    }


def memory_mib():
    try:
        with open('/proc/meminfo') as handle:
            for line in handle:
                if line.startswith('MemTotal'):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def free_gib(path):
    try:
        stat = os.statvfs(path)
    except OSError:
        return 0.0
    return round(stat.f_bavail * stat.f_frsize / (1 << 30), 2)


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ''
