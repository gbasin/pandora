"""Make a machine into a worker, twice if you like.

Idempotence is the whole design. `provision.sh` checks before every action and
says `present`, `created` or `changed` for each one, so a second run is a
no-op with a report that proves it rather than a silent re-application. That
report is what a person reads before a cut-over, and it is why nothing here
prints "done".

The sequence: ship the script, run it, read its survey, compare the survey with
the manifest, run the canary, and only then write `ready`. A worker whose
canary failed keeps whatever state it had, and the reason travels with it.
"""
import shlex
import time

from ..errors import PandoraError
from . import versions
from .remote import Remote

SCRIPT = None                      # filled by `script_text`, cached per process


def script_text():
    global SCRIPT
    if SCRIPT is None:
        from pathlib import Path
        SCRIPT = (Path(__file__).resolve().parent / 'provision.sh').read_text()
    return SCRIPT


def preamble(manifest, *, root, engine_root, pool_file):
    """The variable assignments `provision.sh` reads, quoted for sh."""
    worker = manifest['worker']
    packages = ' '.join('%s=%s' % (name, version)
                        for name, version in sorted(manifest['packages'].items()))
    values = {
        'PACKAGES': packages,
        'PROJECT': worker['project'],
        'POOL': worker['pool'],
        'PROFILE': worker['profile'],
        'BRIDGE': worker['bridge'],
        'SUBNET': worker['subnet'],
        'DEVICE': worker['device'],
        'DISK_FLOOR_GIB': str(worker['disk_floor_gib']),
        'RUN_DISK_GIB': str(worker['run_disk_gib']),
        'MAX_RUNNING': str(worker['max_running']),
        'LOOP_GIB': str(worker['loop_size_gib']),
        'POOL_FILE': pool_file,
        'ROOT': root,
        'ENGINE_ROOT': engine_root,
        'WORKER_USER': worker['user'],
        'UNATTENDED': 'true' if worker['unattended_upgrades'] else 'false',
        'MANIFEST': versions.render(manifest),
        'MANIFEST_DIGEST': versions.digest(manifest),
    }
    return ''.join('%s=%s\n' % (key, shlex.quote(value)) for key, value in sorted(values.items()))


def parse(output):
    """`provision.sh`'s tab-separated lines into steps and facts."""
    steps, found = [], {}
    for line in output.splitlines():
        parts = line.split('\t')
        if parts[0] == 'STEP' and len(parts) >= 4:
            steps.append({'state': parts[1], 'step': parts[2], 'detail': parts[3]})
        elif parts[0] == 'FACT' and len(parts) >= 3:
            found[parts[1]] = parts[2]
    return steps, found


def apply(remote, manifest, *, root, engine_root, timeout=1800):
    """Run the script once. Returns (steps, facts)."""
    text = preamble(manifest, root=root, engine_root=engine_root,
                    pool_file=root.rstrip('/') + '/pool.img') + script_text()
    code, out, err = remote.link.run(['sh', '-s'], stdin=text.encode(),
                                     timeout=timeout, check=False)
    steps, found = parse(out)
    if code != 0:
        raise PandoraError('provision.sh exited %d after %d step(s): %s'
                           % (code, len(steps), (err.strip() or out.strip())[-600:]))
    return steps, found


def run(host, *, manifest, control_dir, root=None, engine_root=None, canary=None,
        skip_canary=False, timeout=1800, notice=print):
    """Provision, survey, gate on the canary, and write the ready state."""
    worker = manifest['worker']
    started = time.monotonic()
    remote = Remote(host, control_dir=control_dir,
                    engine_root=engine_root or worker['engine_root'])
    try:
        root = remote.expand(root or worker['root'])
        engine_root = remote.root()
        manifest['worker'] = dict(worker, root=root, engine_root=engine_root)
        notice('provisioning %s: root %s, engine %s' % (host, root, engine_root))
        steps, found = apply(remote, manifest, root=root, engine_root=engine_root,
                             timeout=timeout)
        changed = [item for item in steps if item['state'] in ('created', 'changed')]
        failed = [item for item in steps if item['state'] == 'failed']
        report = {'ok': not failed, 'host': host, 'root': root, 'engine_root': engine_root,
                  'manifest_digest': versions.digest(manifest), 'steps': steps,
                  'changed': len(changed), 'facts': found,
                  'seconds': round(time.monotonic() - started, 1)}
        if failed:
            report['reason'] = '; '.join('%s: %s' % (item['step'], item['detail'])
                                         for item in failed)
            return report
        status = remote.worker(['--root', root, '--engine-root', engine_root, 'status'],
                               timeout=300)
        report['drift'] = status.get('drift') or []
        if skip_canary:
            report['canary'] = {'ok': None, 'reason': 'skipped by --no-canary'}
            remote.worker(['--root', root, 'ready', '--set', 'unproven',
                           '--reason', 'provisioned, canary skipped'], timeout=120)
            return report
        argv = ['--root', root, '--engine-root', engine_root, 'canary', '--mark']
        for flag, value in sorted((canary or {}).items()):
            if value is None:
                continue
            argv += ['--' + flag.replace('_', '-'), str(value)] if value is not True \
                else ['--' + flag.replace('_', '-')]
        verdict = remote.worker(argv, timeout=max(timeout, 900))
        report['canary'] = verdict
        report['ok'] = bool(verdict.get('ok')) and not report['drift']
        report['state'] = remote.worker(['--root', root, 'status'],
                                        timeout=300).get('state')
        return report
    finally:
        remote.close()


def render(report):
    """The human report: what changed, what drifted, what the canary said."""
    lines = ['%-9s %-26s %s' % ('state', 'step', 'detail')]
    for item in report.get('steps') or []:
        lines.append('%-9s %-26s %s' % (item['state'], item['step'], item['detail'][:80]))
    lines.append('')
    lines.append('%d step(s), %d changed, %.1fs'
                 % (len(report.get('steps') or []), report.get('changed', 0),
                    report.get('seconds', 0)))
    for item in report.get('drift') or []:
        lines.append('drift: %s %s wanted %s, has %s (%s)'
                     % (item['kind'], item['name'], item['want'], item['have'], item['detail']))
    verdict = report.get('canary') or {}
    if verdict.get('ok') is None:
        lines.append('canary: %s' % verdict.get('reason', 'not run'))
    else:
        for row in verdict.get('checks') or []:
            lines.append('%-4s %-46s %6.1fs %s'
                         % ('ok' if row['ok'] else 'FAIL', row['check'], row['at'],
                            row['detail'][:70]))
        lines.append('canary: %s, %d failure(s) in %.1fs'
                     % ('pass' if verdict.get('ok') else 'FAIL',
                        verdict.get('failures', 0), verdict.get('seconds', 0)))
    if report.get('state'):
        lines.append('worker state: ' + report['state'])
    if report.get('reason'):
        lines.append('reason: ' + report['reason'])
    return '\n'.join(lines)
