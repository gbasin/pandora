"""`pandora`: the one command a person types.

    pandora daemon                      run the client daemon in the foreground
    pandora enrol <repo> [--config F]   mark a repository routable, all worktrees
    pandora unenrol <repo>
    pandora run -- <argv>               what the shim calls
    pandora wait <id> [--max-wait S]    re-attach to a run
    pandora ps                          what has run, and what is running
    pandora logs <id>                   one run's output
    pandora result <id>                 one run's result JSON
    pandora cancel <id>
    pandora stats                       routed and, just as importantly, not routed
    pandora worker provision            make a host into a worker, idempotently
    pandora worker status               versions, drift, pool, goldens, ready state
    pandora worker canary               the worker's own health gate
    pandora worker gc [--dry-run]       leaked instances, volumes, old goldens
    pandora worker goldens              what is baked in, and what it cost
    pandora worker reconcile            after an engine restart

Everything Pandora says about itself goes to stderr, prefixed `pandora:`, so a
caller piping stdout gets the command's output and nothing else.
"""
import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

from .client import enrolment, settings
from .client.protocol import Reader, VERSION, dump
from .config import classify as classifier
from .config import loader
from .errors import ConfigError, PandoraError
from .exits import INFRA, STILL_RUNNING


def notice(text):
    sys.stderr.write('pandora: ' + text + '\n')
    sys.stderr.flush()


def ask(sock_path, request, timeout=30.0):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(sock_path))
    sock.sendall(dump({'v': VERSION, **request}))
    reader = Reader(sock)
    try:
        return reader.line()
    finally:
        sock.close()


def state_of(args):
    config = settings.load(args.config)
    return Path(args.state or config['client']['state']).expanduser(), config


# -- commands ---------------------------------------------------------------

def cmd_daemon(args):
    from .client import daemon
    forward = []
    if args.state:
        forward += ['--state', str(args.state)]
    if args.config:
        forward += ['--config', args.config]
    return daemon.main(forward)


def cmd_enrol(args):
    """Write the marker into the repository's git common directory.

    One file enrols every worktree of the repository at once, including ones
    created tomorrow, because every worktree shares one common directory.
    """
    state, config = state_of(args)
    common = enrolment.common_dir(args.repo)
    if common is None:
        notice('not a git repository: %s' % args.repo)
        return 1
    try:
        path, origin = loader.resolve(args.repo, args.config_toml)
        repo_config = loader.load(path)
    except ConfigError as error:
        notice(str(error))
        return 1
    claims = classifier.claim_index(repo_config)
    text = enrolment.render(socket_path=str(state / 'client.sock'),
                            repo=args.name or repo_config['repo']['name'],
                            claims=claims,
                            policies=classifier.policy_index(repo_config),
                            strip_prefixes=repo_config['matching']['strip_prefixes'],
                            origin=str(path),
                            home=str(Path(__file__).resolve().parents[1]))
    marker = enrolment.write(common, text)
    notice('enrolled %s from %s (%s): %d claimed form%s, marker %s'
           % (repo_config['repo']['name'], path, origin, len(claims),
              '' if len(claims) == 1 else 's', marker))
    notice('add this to %s if it is not there yet:\n'
           '  [[repos]]\n  name = "%s"\n  root = "%s"\n  config = "%s"'
           % (config.get('source') or settings.DEFAULT_PATH,
              repo_config['repo']['name'], Path(args.repo).resolve(),
              '' if origin == 'repo-root' else path))
    return 0


def cmd_unenrol(args):
    common = enrolment.common_dir(args.repo)
    marker = Path(common or '.') / enrolment.MARKER
    if marker.is_file():
        marker.unlink()
        notice('removed ' + str(marker))
    else:
        notice('no marker at ' + str(marker))
    return 0


def cmd_run(args):
    from .client import shim
    state, _ = state_of(args)
    command = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    return shim.main(['--sock', str(state / 'client.sock'),
                      '--real', args.real or os.environ.get('PANDORA_REAL_PNPM', 'pnpm'),
                      '--state', str(state), '--', *command])


def cmd_wait(args):
    """Re-attach to a run the shim detached from, and exit as it exits."""
    from .client import shim
    state, _ = state_of(args)
    sock_path = state / 'client.sock'
    try:
        sock = shim.connect(str(sock_path), timeout=5.0)
    except OSError as error:
        notice('daemon unreachable: %s' % error)
        return INFRA
    sock.sendall(dump({'v': VERSION, 'op': 'attach', 'run': args.run, 'from': 0}))
    reader = Reader(sock)
    frame = reader.line()
    if frame is None or frame.get('t') != 'accepted':
        notice('cannot attach to %s: %s' % (args.run, frame))
        return INFRA
    sock.settimeout(None)
    stream = shim.Stream(str(sock_path), args.run, reader, sock)
    if args.max_wait:
        # A deadline changes what the caller learns, not what the run does: the
        # run keeps going and 124 says so.
        import threading
        timer = threading.Timer(args.max_wait, lambda: sock.close())
        timer.daemon = True
        timer.start()
    code = stream.pump()
    if code is None:
        if args.max_wait:
            notice('run %s is still going after %ss; it was not stopped. '
                   'Re-attach with: pandora wait %s' % (args.run, args.max_wait, args.run))
            return STILL_RUNNING
        return INFRA
    return code


def cmd_ps(args):
    state, _ = state_of(args)
    pause = {}
    try:
        answer = ask(state / 'client.sock', {'op': 'ps'})
        rows, pause = answer['data'], answer.get('pause') or {}
    except OSError:
        rows = []
        for meta in sorted((state / 'runs').glob('*/meta.json')):
            try:
                rows.append(json.loads(meta.read_text()))
            except (OSError, ValueError):
                continue
        rows.sort(key=lambda row: row.get('started', 0), reverse=True)
    if args.json:
        print(json.dumps({'runs': rows, 'pause': pause} if pause else rows,
                         indent=1, sort_keys=True))
        return 0
    if pause.get('paused'):
        # First line, not a footnote: a queue that is not admitting is the most
        # important fact on the screen.
        print('local lane PAUSED: %s (max wait %gs)'
              % (pause.get('evidence'), pause.get('max_wait_seconds', 0)))
    print('%-14s %-6s %-10s %-16s %5s  %s'
          % ('run', 'lane', 'state', 'remote', 'exit', 'command'))
    for row in rows[:args.limit]:
        print('%-14s %-6s %-10s %-16s %5s  %s' % (
            row.get('id', '')[:14], (row.get('lane') or 'remote')[:6],
            (row.get('state') or '')[:10], (row.get('remote') or '-')[:16],
            '-' if row.get('exit_code') is None else row['exit_code'],
            ' '.join(row.get('argv') or [])[:60]))
    return 0


def cmd_logs(args):
    state, _ = state_of(args)
    path = state / 'runs' / args.run / 'log'
    if not path.is_file():
        notice('no log for run ' + args.run)
        return 1
    import base64
    with path.open('rb') as handle:
        for line in handle:
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if frame.get('t') == 'log':
                sys.stdout.buffer.write(base64.b64decode(frame['b64']))
    sys.stdout.buffer.flush()
    return 0


def cmd_result(args):
    state, _ = state_of(args)
    path = state / 'runs' / args.run / 'result.json'
    if not path.is_file():
        notice('no result for run %s (still running, or it never reached the worker)' % args.run)
        return 1
    print(path.read_text().rstrip())
    return 0


def cmd_cancel(args):
    state, _ = state_of(args)
    try:
        answer = ask(state / 'client.sock', {'op': 'cancel', 'run': args.run})
    except OSError as error:
        notice('daemon unreachable: %s' % error)
        return INFRA
    notice('cancel requested for %s: %s' % (args.run, json.dumps(answer)))
    return 0


def percentile(values, fraction):
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def cmd_stats(args):
    state, _ = state_of(args)
    try:
        data = ask(state / 'client.sock', {'op': 'stats'}, timeout=60.0)['data']
    except OSError as error:
        notice('daemon unreachable: %s' % error)
        return INFRA
    if args.json:
        print(json.dumps(data, indent=1, sort_keys=True))
        return 0
    routed = data['runs']
    print('routed runs: %d' % len(routed))
    by_state = {}
    for row in routed:
        by_state[row.get('state') or '?'] = by_state.get(row.get('state') or '?', 0) + 1
    for name, count in sorted(by_state.items()):
        print('  %-18s %d' % (name, count))
    rows = data['passthrough']
    print('local, not routed: %d' % len(rows))
    groups = {}
    for row in rows:
        key = ' '.join(row.get('argv', [])[:2]) or '(unknown)'
        groups.setdefault(key, []).append(row)
    if groups:
        print('  %-28s %5s %9s %9s %9s  %s'
              % ('command', 'runs', 'p50 ms', 'p95 ms', 'total s', 'why'))
        for key, group in sorted(groups.items(),
                                 key=lambda item: -sum(row.get('duration_ms', 0)
                                                       for row in item[1])):
            durations = [row.get('duration_ms', 0) for row in group]
            why = ', '.join(sorted({row.get('reason') or row.get('kind', '') for row in group}))
            print('  %-28s %5d %9d %9d %9.1f  %s'
                  % (key, len(group), percentile(durations, 0.5), percentile(durations, 0.95),
                     sum(durations) / 1000.0, why))
    local = data.get('local') or {}
    if local:
        print('local lane: %d MiB held of %d, %d running%s'
              % (local.get('held_mib', 0), local.get('budget_mib', 0),
                 len(local.get('running') or []),
                 ', singleton %s' % ', '.join(sorted(local['singletons']))
                 if local.get('singletons') else ''))
    pause = local.get('pause') or {}
    if pause:
        print('  pause gate: %s, %d episode(s), %gs paused, %d job(s) delayed, %d refused%s'
              % ('PAUSED (%s)' % pause.get('evidence') if pause.get('paused')
                 else ('open' if pause.get('enabled') else 'disabled'),
                 pause.get('episodes', 0), pause.get('paused_seconds', 0),
                 pause.get('jobs_delayed', 0), pause.get('jobs_refused', 0),
                 '; last %s' % pause['last_evidence'] if pause.get('last_evidence') else ''))
    worker = data.get('worker') or {}
    if worker.get('ok'):
        scheduler = worker['scheduler']
        print('worker: %d MiB held of %d, %d lane(s), PANDORA_CPUS now %d'
              % (scheduler['held_mib'], scheduler['budget_mib'], scheduler['lanes'],
                 scheduler['cpus_hint_now']))
        for item in worker.get('reservations', []):
            print('  %-10s %-20s reserve %5d MiB  ceiling %5d  class %-6s  %d sample(s)'
                  % (item['repo'], item['job'], item['reservation_mib'], item['ceiling_mib'],
                     item['size_class'], item['samples']))
    elif worker:
        print('worker: unreachable (%s)' % worker.get('error'))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='pandora', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--state', default=None)
    parser.add_argument('--config', default=None, help='path to config.toml')
    sub = parser.add_subparsers(dest='which', required=True)

    sub.add_parser('daemon', help='run the client daemon in the foreground'
                   ).set_defaults(func=cmd_daemon)

    enrol = sub.add_parser('enrol', help='mark a repository routable, all worktrees at once')
    enrol.add_argument('repo')
    enrol.add_argument('--name', default=None)
    enrol.add_argument('--config', dest='config_toml', default=None,
                       help='a pandora.toml to use when the repository has none')
    enrol.set_defaults(func=cmd_enrol)

    unenrol = sub.add_parser('unenrol')
    unenrol.add_argument('repo')
    unenrol.set_defaults(func=cmd_unenrol)

    run = sub.add_parser('run', help='what the shim calls')
    run.add_argument('--real', default=None)
    run.add_argument('argv', nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    wait = sub.add_parser('wait', help='re-attach to a run')
    wait.add_argument('run')
    wait.add_argument('--max-wait', type=float, default=0)
    wait.set_defaults(func=cmd_wait)

    ps = sub.add_parser('ps')
    ps.add_argument('--limit', type=int, default=20)
    ps.add_argument('--json', action='store_true')
    ps.set_defaults(func=cmd_ps)

    for name, function in (('logs', cmd_logs), ('result', cmd_result), ('cancel', cmd_cancel)):
        node = sub.add_parser(name)
        node.add_argument('run')
        node.set_defaults(func=function)

    stats = sub.add_parser('stats')
    stats.add_argument('--json', action='store_true')
    stats.set_defaults(func=cmd_stats)

    # `pandora worker ...` is about the machine rather than the run, and it has
    # to work on a host no client has adopted yet, so it owns its own parser.
    from .worker import cli as worker_cli
    worker_cli.add_parser(sub)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PandoraError as error:
        notice('%s: %s' % (type(error).__name__, error))
        return INFRA


if __name__ == '__main__':
    raise SystemExit(main())
