"""Preview the actual frozen input without submission or persistent writes.

Bytes are observed regular-file sizes, not an estimate of rsync traffic.
Saved same-input history describes source identity, not an equivalent command
or a reusable test verdict.
"""
from pathlib import Path

from ..errors import SnapshotError
from ..snapshot import freeze as snapshot
from .runindex import RunIndex


def same_input(state, repo, input_id):
    """The newest retained matching run, or None; malformed rows are ignored."""
    if state is None:
        return None
    index = RunIndex(Path(state) / 'runs')
    for run_id in index.newest():
        # Do not learn this record into the daemon's in-memory row cache: an
        # old or malformed saved `id` is irrelevant to directory identity.
        meta = index.read(index.runs / run_id / 'meta.json')
        if not isinstance(meta, dict) or meta.get('repo') != repo:
            continue
        result = index.result(run_id)
        if isinstance(result, dict) and result.get('input_id') == input_id:
            return run_id
    return None


def build(root, *, config, state=None, history_repo=None):
    """Freeze with the same exclusion rules as submission, without a disk cache."""
    root = Path(root).resolve()
    missing = []
    try:
        records, dropped, identity = snapshot.freeze(
            root, exclude_globs=config['secrets']['exclude_globs'], cache=None, missing=missing)
    except OSError as error:
        raise SnapshotError('cannot capture source: %s' % error) from None
    directories = {}
    total = 0
    for record in records:
        name = record['path']
        size = 0
        if 'link' not in record:
            try:
                size = (root / name).stat().st_size
            except OSError as error:
                raise SnapshotError('cannot measure source file %s: %s' % (name, error)) from None
        group = name.split('/', 1)[0] if '/' in name else '.'
        row = directories.setdefault(group, {'path': group, 'files': 0, 'bytes': 0})
        row['files'] += 1
        row['bytes'] += size
        total += size
    repo = config['repo']['name']
    return {'worktree': str(root), 'repo': repo, 'input_id': identity,
            'files': len(records), 'bytes': total,
            'directories': sorted(directories.values(), key=lambda row: (-row['bytes'], row['path'])),
            'excluded': dropped, 'missing': missing,
            'submission_allowed': not (len(missing) > 50 or len(missing) > len(records)),
            'same_input_run': same_input(state, history_repo or repo, identity)}


def render(report):
    """Render observed source counts and retained history, with no wire estimate."""
    lines = ['manifest for %s (%s)' % (report['repo'], report['worktree']),
             '  input %s' % report['input_id'],
             '  %s entries, %s source bytes (observed; not transfer bytes)'
             % (format(report['files'], ','), format(report['bytes'], ',')),
             '  top-level directories (root files: .):']
    for row in report['directories']:
        lines.append('    %s: %s entries, %s bytes'
                     % (row['path'], format(row['files'], ','), format(row['bytes'], ',')))
    lines.append('  excluded: %s' % len(report['excluded']))
    lines.extend('    ' + name for name in report['excluded'])
    lines.append('  missing tracked files: %s' % len(report['missing']))
    lines.extend('    ' + name for name in report['missing'])
    if not report['submission_allowed']:
        lines.append('  remote submission would refuse this partial tree; restore missing '
                     'files or commit their deletions')
    if report['same_input_run']:
        lines.append('  same input as saved run %s (source identity only, not command or verdict)'
                     % report['same_input_run'])
    else:
        lines.append('  no matching run in retained local history')
    cache = report.get('worker_cache')
    if cache is not None:
        lines.append('  worker cache: %s' % cache['status'])
        if cache['status'] == 'absent':
            lines.append('    source transfer would be needed; wire bytes depend on rsync reuse')
        if cache.get('grace_refreshed'):
            lines.append('    cached input retention grace refreshed')
        if cache.get('error'):
            lines.append('    ' + cache['error'])
    return '\n'.join(lines)


def worker_cache(report, *, config):
    """Probe only: no bundle upload, source transfer, admission, or live state writes.

    The existing allowlisted probe renews a present input's GC grace.
    """
    import tempfile
    from ..errors import PandoraError, TransferError
    from ..snapshot import transfer
    from .worker import Worker

    try:
        with tempfile.TemporaryDirectory(prefix='pandora-manifest-') as temporary:
            worker = Worker(config['worker']['host'], state=temporary,
                            engine_root=config['worker']['engine_root'],
                            persist=config['worker'].get('ssh_persist', '10m'))
            try:
                root = worker.root()
                final = transfer.cache_paths(root, report['repo'], report['input_id'])['final']
                _, out, _ = worker.link.feed(transfer.FEEDS['probe'], (root, final), timeout=60)
                status = out.strip()
                if status not in ('present', 'absent'):
                    raise TransferError('unexpected worker cache response: %r' % status[:200])
                return {'status': status, 'grace_refreshed': status == 'present'}
            finally:
                worker.link.close()
    except (PandoraError, OSError) as error:
        return {'status': 'unknown', 'grace_refreshed': False, 'error': str(error)}
