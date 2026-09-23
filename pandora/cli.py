"""`pandora`: this repository's heavy commands, run on a Linux worker.

Type the command you would have typed, from the repository root. Pandora routes
it if the repository claims it and otherwise gets out of the way.

INVARIANTS
  * Same cwd, environment and exit code as a local run; `$?` and traps behave.
  * Declared results are in your worktree before the command exits. A missing
    report is reported as missing, never as zero failures.
  * Run from the repository root. In a subdirectory, a routed command whose
    arguments name a path is refused (exit 64) rather than run locally.
  * Exit codes that are not the command's own:
      70  infrastructure failure, never a test verdict
      75  busy or stale: a validation already active here, or the tree changed
     124  `--max-wait` elapsed; the run was NOT stopped
     130  cancelled
  * `PANDORA_OFF=1 <command>` runs it here with no Pandora at all.
    `PANDORA_WHERE=local|remote <command>` moves one run between lanes and keeps
    the queue and the stats; 64 if the job cannot run there, never a fallback.
  * Pandora's own lines go to stderr as `pandora: ...`. The last one may be
    `pandora: hint: ...`: the next action, derived from evidence.

RUNS
  pandora ps [--json]              what is running and what just ran
  pandora wait <id> [--max-wait S] re-attach; exits as the run exits
  pandora logs <id>                replay a run's output
  pandora result <id> [--json]     outcome, exit, hint; --json for everything
  pandora cancel <id>              stop it; a remote instance is destroyed
  pandora stats [--since 24h|7d] [--json]   what routed, waited, fell back

FANOUT (for orchestrators; plain commands never need it)
  pandora run --detach -- <pnpm args>   submit, print the run id, return
  pandora wait <id> <id> ...            one outcome line per id; non-zero if
                                        any did not pass
  PANDORA_SHARDS=8 <command>            shard count for this one run
  pandora result <id> --json            per-shard outcomes and the input digest

MACHINE
  pandora daemon | enrol <repo> | unenrol <repo> | worker <verb>
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
                            heavy=enrolment.heavy_forms(claims),
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
    if command[:1] == ['pnpm']:
        command = command[1:]          # `pandora run -- pnpm journey X` reads naturally
    return shim.main(['--sock', str(state / 'client.sock'),
                      '--real', args.real or os.environ.get('PANDORA_REAL_PNPM', 'pnpm'),
                      '--state', str(state)] + (['--detach'] if args.detach else [])
                     + (['--where', args.where] if args.where else [])
                     + ['--', *command])


def attach(sock_path, run_id, *, quiet=False, deadline=None):
    """Follow one run to its exit. Returns its code, or None if cut off."""
    from .client import shim
    try:
        sock = shim.connect(str(sock_path), timeout=5.0)
    except OSError as error:
        notice('daemon unreachable: %s' % error)
        return INFRA
    sock.sendall(dump({'v': VERSION, 'op': 'attach', 'run': run_id, 'from': 0}))
    reader = Reader(sock)
    frame = reader.line()
    if frame is None or frame.get('t') != 'accepted':
        notice('cannot attach to %s: %s' % (run_id, (frame or {}).get('msg') or frame))
        sock.close()
        return INFRA
    timer = None
    if deadline is not None:
        # A deadline changes what the caller learns, not what the run does: the
        # run keeps going and 124 says so.
        import threading
        timer = threading.Timer(max(0.0, deadline - time.monotonic()), sock.close)
        timer.daemon = True
        timer.start()
    sock.settimeout(None)
    try:
        if not quiet:
            return shim.Stream(str(sock_path), run_id, reader, sock).pump()
        while True:
            try:
                frame = reader.line()
            except (OSError, ValueError):
                return None
            if frame is None:
                return None
            if frame.get('t') == 'exit':
                return int(frame['code'])
    finally:
        if timer is not None:
            timer.cancel()
        sock.close()


def cmd_wait(args):
    """Re-attach to one run and exit as it exits, or to several and summarise.

    With one id the output streams exactly as the original caller saw it. With
    several it would be an interleaving nobody can read, so each run gets one
    line on stdout instead -- id, outcome, exit, hint -- and the exit is the
    first non-zero one in the order given, or 124 if any is still going.
    """
    state, _ = state_of(args)
    sock_path = state / 'client.sock'
    deadline = time.monotonic() + args.max_wait if args.max_wait else None
    if len(args.run) == 1:
        code = attach(sock_path, args.run[0], deadline=deadline)
        if code is None:
            if deadline is not None:
                notice('run %s is still going after %ss; it was not stopped. '
                       'Re-attach with: pandora wait %s' % (args.run[0], args.max_wait,
                                                           args.run[0]))
                return STILL_RUNNING
            return INFRA
        return code
    worst, still = 0, False
    for run_id in args.run:
        code = attach(sock_path, run_id, quiet=True, deadline=deadline)
        meta = read_json(state / 'runs' / run_id / 'meta.json') or {}
        if code is None:
            still = still or deadline is not None
            print('%-14s %-14s %4s' % (run_id, 'still-running' if deadline else 'lost', '-'))
            continue
        print('%-14s %-14s %4d%s' % (run_id, meta.get('state') or '?', code,
                                     '  hint: ' + meta['hint'] if meta.get('hint') else ''))
        if code and not worst:
            worst = code
    sys.stdout.flush()
    return worst or (STILL_RUNNING if still else 0)


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def cmd_ps(args):
    state, _ = state_of(args)
    pause, worker = {}, {}
    try:
        answer = ask(state / 'client.sock', {'op': 'ps'})
        rows = answer['data']
        pause, worker = answer.get('pause') or {}, answer.get('worker') or {}
    except OSError:
        rows = []
        for meta in sorted((state / 'runs').glob('*/meta.json')):
            try:
                rows.append(json.loads(meta.read_text()))
            except (OSError, ValueError):
                continue
        rows.sort(key=lambda row: row.get('started', 0), reverse=True)
        worker = {'worker': 'unknown', 'reason': 'the daemon is not running'}
    if args.json:
        print(json.dumps({'runs': rows, 'pause': pause, 'worker': worker}
                         if pause or worker else rows, indent=1, sort_keys=True))
        return 0
    print(worker_line(worker))
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


def worker_line(worker):
    """The header. `down` is what a reader most needs and it is said first.

    A stale reading reads as `unknown`, not as its last value, because a daemon
    that has not polled since yesterday knows nothing about now -- and a header
    that claims otherwise is worse than one that admits it.
    """
    state = (worker or {}).get('worker') or 'unknown'
    extra = []
    if worker.get('disk'):
        extra.append('disk ' + str(worker['disk']))
    if (worker.get('canary') or {}).get('ok') is False:
        extra.append('CANARY FAILING')
    if worker.get('kernel_drift'):
        extra.append('kernel drift')
    if state in ('down', 'degraded', 'unknown') and worker.get('reason'):
        extra.append(str(worker['reason'])[:80])
    age = worker.get('age_seconds')
    if age is not None and state != 'unknown':
        extra.append('polled %ds ago' % age)
    return 'worker: %s%s' % (state.upper() if state == 'down' else state,
                             ' (' + '; '.join(extra) + ')' if extra else '')


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
    result = read_json(path)
    if result is None:
        notice('no result for run %s (still running, or it never reached the worker)' % args.run)
        return 1
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True))
        return 0
    print(render_result(args.run, result))
    return 0


def render_result(run_id, result):
    """A few lines: the verdict, where it ran, the shards, the hint."""
    lines = ['%s: %s, exit %s, %.1fs, %s lane%s' % (
        run_id, result.get('outcome'), result.get('cli_exit'),
        float(result.get('wall_seconds') or 0), result.get('lane') or 'remote',
        ', peak %s MiB' % result['peak_mib'] if result.get('peak_mib') is not None else '')]
    placed = result.get('placement') or {}
    if placed.get('overridden'):
        lines.append('  placed %s by override; the job says %s'
                     % (placed.get('where'), placed.get('declared')))
    if result.get('input_id'):
        lines.append('  input %s%s' % (result['input_id'],
                                       ' (same as %s)' % result['same_input_as']
                                       if result.get('same_input_as') else ''))
    for row in (result.get('evidence') or {}).get('shards') or []:
        lines.append('  shard %s: %s, exit %s' % (row.get('shard'), row.get('outcome'),
                                                  row.get('exit_code')))
    missing = (result.get('outputs') or {}).get('missing') or []
    if missing:
        lines.append('  missing: ' + ', '.join(missing))
    if result.get('hint'):
        lines.append('  hint: ' + result['hint'])
    return '\n'.join(lines)


def cmd_cancel(args):
    state, _ = state_of(args)
    try:
        answer = ask(state / 'client.sock', {'op': 'cancel', 'run': args.run})
    except OSError as error:
        notice('daemon unreachable: %s' % error)
        return INFRA
    notice('cancel requested for %s: %s' % (args.run, json.dumps(answer)))
    return 0


def cmd_stats(args):
    """One report, whether or not the daemon is up.

    The daemon is asked first, because it is the only process holding the
    worker link and the live queue. When it is not there the report is built
    from the same files it would have read, minus the worker's half -- which is
    exactly when a person most wants to see what has been happening here.
    """
    state, _ = state_of(args)
    from .client import stats as statistics
    try:
        since = statistics.parse_since(args.since)
    except ValueError as error:
        notice(str(error))
        return 1
    try:
        answer = ask(state / 'client.sock', {'op': 'stats', 'since': args.since},
                     timeout=90.0)
        data = (answer or {}).get('data')
        if not isinstance(data, dict) or 'by_job' not in data:
            # An older daemon: no answer, or the pre-v0.2 shape.
            raise OSError('the daemon gave no v0.2 report; restart it to pick one up')
    except OSError as error:
        notice('no report from the daemon (%s); reporting from %s without the worker'
               % (error, state))
        data = statistics.build(state, since=since, window=args.since or 'all')
    if args.json:
        print(json.dumps(data, indent=1, sort_keys=True))
        return 0
    print(statistics.render(data))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='pandora', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--state', default=None)
    parser.add_argument('--config', default=None, help='path to config.toml')
    # The description above is the whole help; argparse's own list of
    # subcommands would repeat it, worse, below the fold.
    sub = parser.add_subparsers(dest='which', required=True, metavar='<command>',
                                help=argparse.SUPPRESS)

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
    run.add_argument('--detach', action='store_true',
                     help='print the run id once accepted and return')
    side = run.add_mutually_exclusive_group()
    side.add_argument('--local', dest='where', action='store_const', const='local',
                      help='run it in the local lane, whatever the job says')
    side.add_argument('--remote', dest='where', action='store_const', const='remote',
                      help='run it on the worker, whatever the job says')
    run.add_argument('argv', nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    wait = sub.add_parser('wait', help='re-attach to one run, or summarise several')
    wait.add_argument('run', nargs='+')
    wait.add_argument('--max-wait', type=float, default=0)
    wait.set_defaults(func=cmd_wait)

    ps = sub.add_parser('ps')
    ps.add_argument('--limit', type=int, default=20)
    ps.add_argument('--json', action='store_true')
    ps.set_defaults(func=cmd_ps)

    for name, function in (('logs', cmd_logs), ('result', cmd_result), ('cancel', cmd_cancel)):
        node = sub.add_parser(name)
        node.add_argument('run')
        if name == 'result':
            node.add_argument('--json', action='store_true')
        node.set_defaults(func=function)

    stats = sub.add_parser('stats', help='what routed, what waited, what did not route')
    stats.add_argument('--since', default=None,
                       help='a window: 24h, 7d, 90m, or a number of seconds. Default: all')
    stats.add_argument('--json', action='store_true')
    stats.set_defaults(func=cmd_stats)

    # `pandora worker ...` is about the machine rather than the run, and it has
    # to work on a host no client has adopted yet, so it owns its own parser.
    from .worker import cli as worker_cli
    worker_cli.add_parser(sub)
    worker_cli.add_cache_parser(sub)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PandoraError as error:
        notice('%s: %s' % (type(error).__name__, error))
        return INFRA


if __name__ == '__main__':
    raise SystemExit(main())
