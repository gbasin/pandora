"""What the worker actually is, read from the worker itself.

`provision.sh` prints the same survey at the end of a provisioning run; this is
the one `pandora worker status` uses afterward, so drift is answered from the
live machine rather than from a file written when it was last touched.
"""
import os
import subprocess
from pathlib import Path


def sh(command, timeout=60):
    proc = subprocess.run(['sh', '-c', command], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=timeout)
    return (proc.stdout or b'').decode('utf-8', 'replace').strip()


def package_versions(names):
    out = {}
    for name in names:
        version = sh("dpkg-query -W -f='${Version}' %s 2>/dev/null" % name)
        out[name] = version or None
    return out


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
    observed = {'unattended_upgrades':
                sh('systemctl is-enabled unattended-upgrades 2>/dev/null') == 'enabled'}
    return {
        'packages': package_versions(sorted(manifest['packages'])),
        'worker': observed,
        'missing': missing,
        'host': {'hostname': sh('hostname'), 'kernel': sh('uname -r'),
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
