"""`pandora`: this repository's heavy commands, run on a Linux worker.

Type the command you would have typed, from the repository root. Pandora routes
it if the repository claims it and otherwise gets out of the way.

INVARIANTS
  * Same cwd, environment and exit code as a local run; `$?` and traps behave.
  * Declared results are in your worktree before the command exits. A missing
    report is reported as missing, never as zero failures.
  * Run from the repository root. Below it, `[matching] subdirectory` re-roots
    a command (64 if an argument names a path), refuses it, or leaves it alone.
  * Exit codes that are not the command's own:
      70  infrastructure failure, never a test verdict (also: daemon installed
          here but not answering after 5 s; nothing ran; run `pandora doctor`)
      75  busy or stale: validation active here, tree changed, or restart ran long
     124  `--max-wait` elapsed; the run was NOT stopped
     130  canceled
  * `--update` runs on the worker, never here; its files come back only from a
    passing run (every shard) over a tree you did not edit, else 75 and a next step.
  * `PANDORA_WHERE=local|remote <command>` moves one run between lanes, in the
    queue; 64 if it cannot run there, never a fallback. `PANDORA_OFF=1`: last resort.
  * Pandora's own lines go to stderr as `pandora: ...`. The last one may be
    `pandora: hint: ...`: the next action, derived from evidence.
  * Each worktree routes by its own pandora.toml; an edit applies on the next command.

RUNS
  pandora ps [--json]              what is running and what just ran
  pandora wait <id> [--max-wait S] re-attach; exits as the run exits
  pandora logs <id>                replay a run's output
  pandora result <id> [--json]     outcome, exit, hint; --json for everything
  pandora cancel <id>              stop it; a remote instance is destroyed
  pandora resolve <id> --keep-local|--take-worker   after an --update conflict
  pandora stats [--since 24h|7d] [--json]   what routed, waited, fell back

FANOUT (for orchestrators; plain commands never need it)
  pandora run --detach -- <pnpm args>   submit, print the run id, return
  pandora wait <id> <id> ...            one outcome line per id; non-zero unless all passed
  PANDORA_SHARDS=8 <command>            shard count for this one run
  pandora result <id> --json            per-shard outcomes and the input digest

MACHINE
  pandora doctor [--json] | enroll <repo> (consent, once) | unenroll <repo> | worker <verb>
  pandora upgrade [--from <checkout> | --version <name>] [--now] | daemon [--install ...]
"""
import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

from .client import enrollment, settings
from .client.protocol import Reader, VERSION, dump
from .config import loader
from .errors import ConfigError, PandoraError, UnknownSchema
from .exits import INFRA, STILL_RUNNING


def notice(text):
    sys.stderr.write('pandora: ' + text + '\n')
    sys.stderr.flush()


# How long an operator verb (`cancel`, `wait`, `doctor`) waits for the
# daemon. A starved daemon at load 90 answered in 66 s on 2026-09-24, and a
# 2-5 s client called it "not running". The shim keeps its 2 s connect: a slow
# daemon must not delay every pnpm call.
OPERATOR_SECONDS = 30.0
PS_SECONDS = 2.0


def ask(sock_path, request, timeout=OPERATOR_SECONDS, *, deadline_seconds=None):
    deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    def remaining():
        if deadline is None:
            return timeout
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('request deadline exceeded')
        return left

    try:
        sock.settimeout(remaining())
        sock.connect(str(sock_path))
        sock.settimeout(remaining())
        sock.sendall(dump({'v': VERSION, **request}))
        return Reader(sock, deadline=deadline).line()
    finally:
        sock.close()


def state_of(args):
    config = settings.load(args.config)
    return Path(args.state or config['client']['state']).expanduser(), config


# -- commands ---------------------------------------------------------------

def restart_drained(args, launchd, label, state):
    """`--restart`: drain, wait for what a restart would end, then kickstart."""
    from .client import drain
    # Before the drain, so a daemon launchd does not run is never left draining.
    agent = launchd.status(label)
    if not agent['loaded']:
        raise launchd.Refused('%s is not loaded in launchd; `pandora daemon --install` first, '
                              'or restart a hand-started daemon by stopping it and starting '
                              'it' % label)
    holder = launchd.lock_holder(state)
    if holder and agent['pid'] != holder:
        # A kickstart would restart launchd's agent, not the daemon that
        # answers: that one would be drained and never restarted.
        raise launchd.Refused('pid %s holds %s but launchd runs %s as pid %s, so a restart '
                              'would not reach it. `pandora daemon --stop`, then `pandora '
                              'daemon --restart`' % (holder, state / 'daemon.lock', label,
                                                     agent['pid']))
    return drain.drain_and_restart(
        state, wait=drain.DEFAULT_RESTART_WAIT if args.wait is None else args.wait,
        now=args.now, say=notice, restart=lambda: launchd.restart(label, say=notice))


def install_drained(args, launchd, label, state, config_path):
    """`--install`: drain the daemon it replaces, as `--restart` does, then load the new plist.

    Nothing to drain when launchd runs no agent and no daemon holds the lock:
    the plist is loaded at once. A daemon launchd did not start is refused
    before any drain, so it is never left draining for a restart that will not
    come. A load that fails after the old daemon has gone clears the draining
    marker: no successor is coming to clear it, and a client should hear "not
    answering" in seconds rather than wait for a restart for eleven minutes.
    """
    from .client import drain

    def load():
        try:
            launchd.install(label, config_path=config_path, state=state,
                            state_arg=bool(args.state), say=notice)
        except launchd.Refused:
            if not launchd.lock_holder(state):
                drain.clear_marker(state)
            raise

    before = launchd.check_install(label, state=state)
    if not before['loaded'] and not launchd.lock_holder(state):
        load()
        return 0
    return drain.drain_and_restart(
        state, wait=drain.DEFAULT_RESTART_WAIT if args.wait is None else args.wait,
        now=args.now, say=notice, restart=load, again='`pandora daemon --install --now`')


def cmd_daemon(args):
    """Run the daemon in the foreground, or manage the launchd agent that runs it.

    `--install` writes a user agent that keeps the daemon running from
    `<data>/current` once `pandora upgrade` has run, else from this checkout,
    and replaces a loaded one after a drain; `--restart` drains and makes it
    load the code its plist names. See `client/launchd.py`
    for what the plist carries and why.
    """
    verb = args.install or args.uninstall or args.restart or args.stop
    if (args.wait is not None or args.now) and not (args.restart or args.install):
        notice('--wait and --now go with --restart or --install')
        return 64
    if verb:
        return cmd_daemon_supervision(args)
    if args.label:
        notice('--label goes with --install, --uninstall, --restart or --stop')
        return 64
    from .client import daemon
    forward = []
    if args.state:
        forward += ['--state', str(args.state)]
    if args.config:
        forward += ['--config', args.config]
    return daemon.main(forward)


def cmd_daemon_supervision(args):
    from .client import launchd
    if sys.platform != 'darwin':
        notice('launchd is macOS only; run `pandora daemon` under your own supervisor')
        return 64
    config_path = Path(args.config or os.environ.get('PANDORA_CONFIG')
                       or settings.DEFAULT_PATH).expanduser().resolve()
    state, _config = state_of(args)
    label = launchd.label_for(state, args.label)
    try:
        if args.install:
            return install_drained(args, launchd, label, state, config_path)
        elif args.uninstall:
            launchd.uninstall(label, state=state, say=notice)
        elif args.restart:
            return restart_drained(args, launchd, label, state)
        else:
            launchd.stop(label, state=state, say=notice)
    except launchd.Refused as error:
        notice(str(error))
        return 1
    return 0


def cmd_upgrade(args):
    """Snapshot the checkout into `<data>/versions`, drain the daemon, flip `current`, restart.

    See `client/install.py` for the layout and why nothing runs from the checkout.
    """
    from .client import install
    state, _config = state_of(args)
    if args.keep < 2:
        notice('--keep must be at least 2: current, and the version to go back to')
        return 64
    if args.version and (args.source or args.dirty):
        notice('--version installs a version already built; it takes no --from or --dirty')
        return 64
    try:
        return install.upgrade(state=state, source=args.source, version=args.version,
                               dirty_ok=args.dirty, now=args.now, no_restart=args.no_restart,
                               relink=True if args.relink else None, wait=args.wait,
                               keep=args.keep, say=notice)
    except (install.Refused, OSError) as error:
        notice(str(error))
        return 1


def cmd_enroll(args):
    """Consent, once per repository, to route it: `[[repos]]`, registration, this cache.

    Enrolling says "Pandora may route this repository on this Mac". It is not a
    configuration step: what is routed is each worktree's own `pandora.toml`,
    and nothing here has to be run again after it changes: the daemon
    derives each worktree's claim cache from that worktree's own file, and the
    shim notices a file newer than its cache. What enrolling writes:

    * a `[[repos]]` table appended to the client config, when the repository
      has none (an existing one is left exactly as it is);
    * `<common>/pandora-repo`, which sends every worktree without a cache yet,
      including ones created tomorrow, to the daemon once;
    * this worktree's claim cache, so its first command is fork-free.

    It removes the old `pandora-enrolled` marker, which the new files replace.
    """
    state, config = state_of(args)
    common = enrollment.common_dir(args.repo)
    root = enrollment.worktree_root(args.repo)
    if common is None or root is None:
        notice('not a git repository: %s' % args.repo)
        return 1
    try:
        path, origin = loader.resolve(root, args.config_toml)
        repo_config = loader.load(path)
    except UnknownSchema as error:
        from .client import install
        notice('%s. If the file is right, this Pandora is older than the file needs: %s. If '
               'it is a mistake, fix the file'
               % (error, install.update_fix(str(Path(__file__).resolve().parents[1]))))
        return 1
    except ConfigError as error:
        notice(str(error))
        return 1
    name = args.name or repo_config['repo']['name']
    external = '' if origin == 'repo-root' else str(path)
    config_path = settings.path_of(args.config)
    known = enrollment.repo_entry(config, root)
    clash = next((repo for repo in config['repos']
                  if repo['name'] == name and repo is not known), None)
    if clash is not None:
        # Another repository's entry under this name: taking it over would
        # route this repository by that entry's root and config.
        notice('%s already has [[repos]] %s for another repository, at %s. Enroll this '
               'one under another name with `--name`, or fix that entry first'
               % (config_path, name, clash['root']))
        return 1
    if known is None:
        known = {'name': name, 'root': str(Path(root).resolve()), 'config': external}
        try:
            settings.append_repo(config_path, name, known['root'], external)
        except (ConfigError, OSError) as error:
            notice('could not add [[repos]] %s to %s (%s); add it by hand:\n%s'
                   % (name, config_path, error,
                      settings.repo_block(name, known['root'], external)))
            return 1
        notice('added [[repos]] %s to %s' % (name, config_path))
    elif known['name'] != name or (external and known.get('config') != external):
        # The entry is what the daemon routes by, so it is also what the cache
        # is derived from: never the --config typed here.
        notice('%s already has [[repos]] %s at %s, left as it is, and routing follows it; '
               'if it is not what you meant, change it to:\n%s'
               % (config_path, known['name'], known['root'],
                  settings.repo_block(name, Path(root).resolve(), external)))
    socket_path = str(state / 'client.sock')
    enrollment.write(common, enrollment.registration_text(
        socket_path=socket_path, repo=known['name']), enrollment.REGISTRATION)
    cache = enrollment.cache_path(root)
    text, sources = enrollment.derive(root, known, socket_path=socket_path,
                                      client=str(config_path))
    enrollment.write_cache(cache, text, sources)
    claims = enrollment.parse(text)['claim']
    legacy = Path(common) / enrollment.MARKER
    if legacy.is_file():
        legacy.unlink()
        notice('removed the old marker %s; the files below replace it' % legacy)
    notice('enrolled %s: %d claimed form%s here; registration %s, claim cache %s. Other '
           'worktrees derive theirs from their own %s on their first command; after a '
           'change to it, the next command takes effect with no enroll'
           % (known['name'], len(claims), '' if len(claims) == 1 else 's',
              Path(common) / enrollment.REGISTRATION, cache, loader.FILENAME))
    check_daemon_knows_claims(state / 'client.sock', root)
    return 0


def check_daemon_knows_claims(sock_path, root):
    """Say so when the daemon cannot refresh caches: every uncached command would pay for it."""
    try:
        answer = ask(sock_path, {'op': 'claims', 'cwd': str(root), 'argv': []}, timeout=5.0)
    except OSError as error:
        notice('no daemon answers on %s (%s). Claimed commands exit 70 until one does: '
               '`pandora daemon --install`' % (sock_path, error))
        return
    if (answer or {}).get('t') == 'draining':
        notice('the daemon on %s is draining for a restart; the next claimed command in '
               'this worktree writes its cache' % sock_path)
        return
    if (answer or {}).get('t') != 'claims':
        notice('the daemon on %s predates claim caches. Until it restarts, every command '
               'in a worktree without a cache costs a Python start and two daemon round '
               'trips. Check `pandora ps`, then run `pandora daemon --restart`' % sock_path)


def cmd_unenroll(args):
    """Remove the registration, the old marker and every claim cache under the common dir."""
    common = enrollment.common_dir(args.repo)
    if common is None:
        notice('not a git repository: %s' % args.repo)
        return 1
    removed = []
    paths = [Path(common) / enrollment.REGISTRATION, Path(common) / enrollment.MARKER]
    paths += [cache for _root, cache in enrollment.caches_of(common)]
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed.append(path)
        notice('removed ' + str(path))
    if not removed:
        notice('nothing to remove under ' + str(common))
    try:
        config = settings.load(args.config)
    except ConfigError:
        return 0                          # the files are gone; the entry is not ours to judge
    known = next((repo for repo in config['repos']
                  if enrollment.common_dir(repo['root']) == common), None)
    if known is not None:
        notice('[[repos]] %s is still in %s; the shim routes nothing here now, and '
               '`pandora run` still does until you remove it'
               % (known['name'], config.get('source') or settings.DEFAULT_PATH))
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
        sock = shim.connect(str(sock_path), timeout=OPERATOR_SECONDS)
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
    if frame.get('owned') is False:
        # Nothing in the daemon will bring it to an exit; waiting would be forever.
        notice('run %s is live on disk but the daemon is not following it; check '
               '`pandora ps`' % run_id)
        sock.close()
        return INFRA
    if not quiet and frame.get('phase'):
        notice(frame['phase'])
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
    """Re-attach to one run and exit as it exits, or to several and summarize.

    With one id the output streams exactly as the original caller saw it. With
    several it would be an interleaving nobody can read, so each run gets one
    line on stdout instead -- id, outcome, exit, hint -- and the exit is the
    first non-zero one in the order given, or 124 if any is still going.

    A run whose stream ended without an exit frame, before any deadline, is
    `lost` and counts as 70: nobody saw it finish, and `--help` promises
    non-zero unless every run passed.
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
            if deadline is not None and time.monotonic() >= deadline:
                still = True
                print('%-14s %-14s %4s' % (run_id, 'still-running', '-'))
            else:
                print('%-14s %-14s %4d' % (run_id, 'lost', INFRA))
                worst = worst or INFRA
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
    from .client import drain
    state, _ = state_of(args)
    pause, worker, me, draining = {}, {}, None, None
    try:
        answer = ask(state / 'client.sock', {'op': 'ps', 'limit': args.limit},
                     deadline_seconds=PS_SECONDS)
        if (not isinstance(answer, dict) or answer.get('t') != 'ps'
                or not isinstance(answer.get('data'), list)):
            raise ValueError((answer or {}).get('msg', 'invalid status response')
                             if isinstance(answer, dict) else 'no status response')
        rows = answer['data']
        pause, worker = answer.get('pause') or {}, answer.get('worker') or {}
        me = answer.get('client')
        draining = answer.get('draining')
    except (OSError, ValueError) as error:
        marker = drain.read_marker(state)
        if marker is not None and marker['age'] < drain.STALE_SECONDS:
            # The restart gap: the old daemon has gone and the new one is not up.
            draining = dict(marker, gap=True)
        reason = ('unresponsive: no status response within %gs' % PS_SECONDS
                  if isinstance(error, TimeoutError) else 'status unavailable: %s' % error)
        if args.json:
            print(json.dumps({'runs': [], 'pause': {}, 'worker': {'worker': 'unknown'},
                              'client': None, 'draining': draining,
                              'daemon': {'responding': False, 'reason': reason}}))
        else:
            if draining:
                print(draining_line(draining))
            print('daemon: %s; run status is unknown' % reason)
        return INFRA
    # Older daemons ignore the requested limit. Keep the same display contract
    # while upgrading: every active row, plus only the requested recent rows.
    active = [row for row in rows if row.get('state') in ('queued', 'running')]
    recent = [row for row in rows if row.get('state') not in ('queued', 'running')]
    rows = active + recent[:args.limit]
    if args.json:
        print(json.dumps({'runs': rows, 'pause': pause, 'worker': worker, 'client': me,
                          'draining': draining, 'daemon': {'responding': True}},
                         indent=1, sort_keys=True))
        return 0
    if draining:
        # First: every command typed now waits for the restart, and says so.
        print(draining_line(draining))
    print(worker_line(worker, me))
    if pause.get('enabled'):
        age = pause.get('age_seconds')
        freshness = ('sample age unknown' if age is None else
                     'sampled %gs ago%s' % (round(age, 1), '; stale' if pause.get('stale') else ''))
        if pause.get('paused'):
            print('local lane PAUSED: %s (max wait %gs; %s)'
                  % (pause.get('evidence'), pause.get('max_wait_seconds', 0), freshness))
        else:
            print('local pressure: %s (%s)' % ('unknown' if age is None else 'last sample clear',
                                               freshness))
    print('%-14s %-6s %-10s %-16s %5s  %s'
          % ('run', 'lane', 'state', 'remote', 'exit', 'command'))
    for row in rows:
        print('%-14s %-6s %-10s %-16s %5s  %s' % (
            row.get('id', '')[:14], (row.get('lane') or 'remote')[:6],
            state_word(row)[:10], (row.get('remote') or '-')[:16],
            '-' if row.get('exit_code') is None else row['exit_code'],
            ' '.join(row.get('argv') or [])[:60]))
    return 0


def ps_limit(value):
    from .client.status import RECENT_LIMIT
    count = int(value)
    if not 0 <= count <= RECENT_LIMIT:
        raise argparse.ArgumentTypeError('recent run limit must be between 0 and %d' % RECENT_LIMIT)
    return count


def draining_line(draining):
    since = draining.get('since')
    ago = ' for %ds' % max(0, time.time() - since) if isinstance(since, (int, float)) else ''
    if draining.get('gap'):
        return ('daemon: draining%s; restarting, no daemon answers yet. New commands wait '
                'for the next one' % ago)
    return ('daemon: draining%s for a restart (asked by pid %s); new commands wait, running '
            'ones finish' % (ago, draining.get('pid') or '?'))


# A queued remote row's pre-accept step, as `ps` shows it: `remote shipping`.
PRE_ACCEPT = {'freeze': 'freezing', 'ship': 'shipping', 'submit': 'submitting'}


def state_word(row):
    state = row.get('state') or ''
    if state == 'queued' and row.get('phase') in PRE_ACCEPT:
        return PRE_ACCEPT[row['phase']]
    return state


def worker_line(worker, me=None):
    """The header. `down` is what a reader most needs and it is said first.

    `me` is this daemon's client name. A worker other Macs share says how many
    runs each other client has live on it, and the line ends `as <me>`.

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
    others = {name: count for name, count in
              ((worker.get('health') or {}).get('live_by_client') or {}).items()
              if name != me and count}
    if others:
        extra.append('live from other clients: ' + ', '.join(
            '%s %d' % item for item in sorted(others.items())))
    return 'worker: %s%s%s' % (state.upper() if state == 'down' else state,
                               ' (' + '; '.join(extra) + ')' if extra else '',
                               ' as ' + me if me else '')


def cmd_logs(args):
    state, _ = state_of(args)
    path = state / 'runs' / args.run / 'log'
    if not path.is_file():
        notice('no log for run ' + args.run)
        return 1
    import base64
    who = submitted(read_json(path.parent / 'meta.json'))
    if who:
        # stderr: stdout is the run's own output, replayed byte for byte.
        notice('run %s was submitted by %s' % (args.run, who))
    with path.open('rb') as handle:
        for line in handle:
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            # `said`: a pre-accept notice the caller saw live, kept in the log.
            if frame.get('t') in ('log', 'said'):
                sys.stdout.buffer.write(base64.b64decode(frame['b64']))
    sys.stdout.buffer.flush()
    return 0


def submitted(meta):
    """`id (via)` for a row that records who submitted it, else None."""
    who = (meta or {}).get('submitter') or {}
    if not who.get('id'):
        return None
    return '%s (%s)' % (who['id'], who.get('via') or '?')


def cmd_result(args):
    state, _ = state_of(args)
    path = state / 'runs' / args.run / 'result.json'
    result = read_json(path)
    meta = read_json(path.parent / 'meta.json')
    if result is None:
        args.state_dir = state
        return result_without_one(args, meta)
    if (meta or {}).get('submitter'):
        # The engine never sees who submitted a run; the row does.
        result.setdefault('submitter', meta['submitter'])
    if (meta or {}).get('client'):
        # A remote result carries the engine's record; a local one has only the row.
        result.setdefault('client', meta['client'])
    if args.json:
        # A result from an engine before the rename has only the old key; both
        # are printed for one release, `same_input_as` as the alias.
        if 'same_input_as' in result and 'same_tree_as' not in result:
            result['same_tree_as'] = result['same_input_as']
        print(json.dumps(result, indent=1, sort_keys=True))
        return 0
    print(render_result(args.run, result))
    return 0


def result_without_one(args, meta):
    """A run that wrote no `result.json`: say what its row does know.

    A refusal before the worker leaves no result, and "no result" used to be
    all `pandora result` said about the four refused runs of 2026-09-24.
    """
    state = (meta or {}).get('state')
    if state is None or state in ('queued', 'running'):
        notice('no result for run %s (still running, or it never reached the worker)' % args.run)
        return 1
    if args.json:
        print(json.dumps(meta, indent=1, sort_keys=True))
    elif state == 'refused' and meta.get('refusal'):
        refusal = meta['refusal']
        print('%s: refused before reaching the worker: %s: %s'
              % (args.run, refusal.get('cause'), refusal.get('detail')))
    elif meta.get('fell_back_to'):
        print('%s: %s; see pandora result %s' % (args.run, state, meta['fell_back_to']))
    else:
        print('%s: %s, exit %s%s' % (args.run, state, meta.get('exit_code'),
                                     ', ' + meta['reason'] if meta.get('reason') else ''))
    if not args.json and submitted(meta):
        print('  submitted by ' + submitted(meta))
    if meta.get('fell_back_to'):
        # The request's verdict is its successor's, not this row's.
        successor = read_json(Path(args.state_dir) / 'runs' / meta['fell_back_to']
                              / 'meta.json') or {}
        code = successor.get('exit_code')
        return code if isinstance(code, int) else 1
    if state == 'refused' and meta.get('refusal'):
        return INFRA
    code = meta.get('exit_code')
    return code if isinstance(code, int) and code else 1


def render_result(run_id, result):
    """A few lines: the verdict, where it ran, the shards, the hint."""
    lines = ['%s: %s, exit %s, %.1fs, %s lane%s' % (
        run_id, result.get('outcome'), result.get('cli_exit'),
        float(result.get('wall_seconds') or 0), result.get('lane') or 'remote',
        ', peak %s MiB' % result['peak_mib'] if result.get('peak_mib') is not None else '')]
    who = submitted(result)
    if who:
        lines.append('  submitted by ' + who)
    if result.get('client'):
        lines.append('  client ' + result['client'])
    placed = result.get('placement') or {}
    if placed.get('overridden'):
        lines.append('  placed %s by override; the job says %s'
                     % (placed.get('where'), placed.get('declared')))
    if result.get('input_id'):
        same = result.get('same_tree_as') or result.get('same_input_as')
        lines.append('  input %s%s' % (result['input_id'],
                                       ' (same tree as %s)' % same if same else ''))
    for number, attempt in enumerate(result.get('attempts') or [], 1):
        lines.append('  attempt %d %s: %s%s' % (
            number, attempt.get('remote'), attempt.get('outcome'),
            ' (%s)' % attempt['cause'] if attempt.get('cause') else ''))
    for row in (result.get('evidence') or {}).get('shards') or []:
        lines.append('  shard %s: %s, exit %s%s' % (
            row.get('shard'), row.get('outcome'), row.get('exit_code'),
            ', retried after %s' % row['retry_cause'] if row.get('retry_cause') else ''))
    flaky = result.get('flaky') or {}
    if flaky.get('order') or flaky.get('shards'):
        lines.append('  flaky: %s' % ', '.join(
            (['whole run %s (failed %s, passed %s)' % (flaky['order'], flaky['failed'],
                                                       flaky['passed'])]
             if flaky.get('order') else [])
            + ['shard %s %s' % (item['shard'], item['order'])
               for item in flaky.get('shards') or []]))
    missing = (result.get('outputs') or {}).get('missing') or []
    if missing:
        lines.append('  missing: ' + ', '.join(missing))
    record = result.get('writeback') or {}
    if record.get('state'):
        lines.append('  write-back: %s%s' % (record['state'],
                                             ', ' + record['why'] if record.get('why') else ''))
        for path in record.get('written') or []:
            lines.append('    wrote %s' % path)
        for item in record.get('conflicts') or []:
            lines.append('    %s: yours kept; proposed %s/%s'
                         % (item['path'], record.get('proposed'), item['path']))
    if result.get('hint'):
        lines.append('  hint: ' + result['hint'])
    return '\n'.join(lines)


def cmd_resolve(args):
    """Settle a `--update` run whose write-back found the declared files edited here.

    No daemon and no worker: the proposal is already in the run directory, and
    resolving is a decision about files on this Mac.
    """
    from .client import writeback
    state, _ = state_of(args)
    run_dir = state / 'runs' / args.run
    result = read_json(run_dir / 'result.json')
    if result is None:
        notice('no result for run %s' % args.run)
        return 1
    code, lines = writeback.resolve(run_dir, result, keep_local=args.keep_local)
    if code == 0:
        temporary = run_dir / 'result.json.tmp'
        temporary.write_text(json.dumps(result, indent=1, sort_keys=True) + '\n')
        temporary.replace(run_dir / 'result.json')
    for line in lines:
        notice(line)
    return code


def cmd_cancel(args):
    state, _ = state_of(args)
    try:
        answer = ask(state / 'client.sock', {'op': 'cancel', 'run': args.run})
    except OSError as error:
        notice('daemon unreachable: %s' % error)
        return INFRA
    notice('cancel requested for %s: %s' % (args.run, json.dumps(answer)))
    return 0


def cmd_doctor(args):
    from .client import doctor
    if args.package_home:
        print(doctor.PACKAGE_HOME)
        return 0
    report = doctor.run(state=args.state, config=args.config)
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
    else:
        print(doctor.render(report))
    return 0 if report['ok'] else 1


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
            raise OSError('the daemon runs older code and gave no report; restart it to pick one up')
    except OSError as error:
        notice('no report from the daemon (%s); reporting from %s without the worker'
               % (error, state))
        data = statistics.build(state, since=since, window=args.since or 'all')
    if args.json:
        print(json.dumps(data, indent=1, sort_keys=True))
        return 0
    print(statistics.render(data))
    return 0


# Old command spellings the parser still accepts, and what each one now is.
DEPRECATED = {'enrol': 'enroll', 'unenrol': 'unenroll'}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='pandora', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--state', default=None)
    parser.add_argument('--config', default=None, help='path to config.toml')
    # The description above is the whole help; argparse's own list of
    # subcommands would repeat it, worse, below the fold.
    sub = parser.add_subparsers(dest='which', required=True, metavar='<command>',
                                help=argparse.SUPPRESS)

    daemon = sub.add_parser('daemon', help='run the client daemon in the foreground, or '
                            'manage the launchd agent that runs it')
    verbs = daemon.add_mutually_exclusive_group()
    verbs.add_argument('--install', action='store_true',
                       help='write a launchd user agent that keeps the daemon running from '
                            '<data>/current (after `pandora upgrade`) or else this checkout, '
                            'and load it; a running daemon is drained first, as --restart does')
    verbs.add_argument('--uninstall', action='store_true',
                       help='unload the agent and delete its plist')
    verbs.add_argument('--restart', action='store_true',
                       help='drain, then launchctl kickstart -k; `pandora upgrade` does it '
                            'too')
    verbs.add_argument('--stop', action='store_true',
                       help='SIGTERM a hand-started daemon and wait up to 10 s')
    daemon.add_argument('--wait', type=float, default=None, metavar='SECONDS',
                        help='--restart, --install: how long to wait for the runs a restart '
                             'would end (default 300)')
    daemon.add_argument('--now', action='store_true',
                        help='--restart, --install: restart when the wait runs out, ending '
                             'those runs')
    daemon.add_argument('--label', default=None,
                        help='the launchd label (default com.pandora.daemon)')
    daemon.set_defaults(func=cmd_daemon)

    upgrade = sub.add_parser('upgrade', help="install the checkout's HEAD as the version "
                             'everything runs, and restart the daemon after a drain')
    upgrade.add_argument('--from', dest='source', default=None, metavar='CHECKOUT',
                         help='the checkout to snapshot (default: the one current came from)')
    upgrade.add_argument('--dirty', action='store_true',
                         help='snapshot uncommitted edits to tracked files instead of refusing')
    upgrade.add_argument('--version', default=None, metavar='NAME',
                         help='install a version already under versions/ (to go back to one)')
    upgrade.add_argument('--now', action='store_true',
                         help='restart the daemon at once: local runs executing and remote runs '
                              'not yet accepted end (a submitting one is looked up on the '
                              'worker); queued local runs are submitted again')
    upgrade.add_argument('--no-restart', action='store_true',
                         help='move current even though the daemon keeps its version until it '
                              'restarts')
    upgrade.add_argument('--relink', action='store_true',
                         help='re-point the launchers on PATH even with a non-default data '
                              'directory')
    upgrade.add_argument('--wait', type=float, default=600, metavar='SECONDS',
                         help='how long the drain waits for the runs a restart would end '
                              '(default 600)')
    upgrade.add_argument('--keep', type=int, default=3,
                         help='versions to keep, current included (default 3, at least 2)')
    upgrade.set_defaults(func=cmd_upgrade)

    # `enrol` and `unenrol` are the old British spellings, kept as hidden aliases
    # for one release so scripts and muscle memory keep working.
    enroll = sub.add_parser('enroll', aliases=['enrol'],
                            help='consent to route a repository: once, for every worktree. '
                                 'What is routed is each worktree\'s own pandora.toml; a '
                                 'change to it needs no enroll',
                            description='Consent, once per repository, for Pandora to route '
                                        'it on this Mac. Enrolling is not configuration: each '
                                        'worktree routes by its own pandora.toml, and a '
                                        'change to that file takes effect on the next command '
                                        'with no enroll.')
    enroll.add_argument('repo')
    enroll.add_argument('--name', default=None)
    enroll.add_argument('--config', dest='config_toml', default=None,
                       help='a pandora.toml to use when the repository has none')
    enroll.set_defaults(func=cmd_enroll)

    unenroll = sub.add_parser('unenroll', aliases=['unenrol'])
    unenroll.add_argument('repo')
    unenroll.set_defaults(func=cmd_unenroll)

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

    wait = sub.add_parser('wait', help='re-attach to one run, or summarize several')
    wait.add_argument('run', nargs='+')
    wait.add_argument('--max-wait', type=float, default=0)
    wait.set_defaults(func=cmd_wait)

    ps = sub.add_parser('ps')
    ps.add_argument('--limit', type=ps_limit, default=20,
                    help='recent completed runs (0–200, default 20); active runs always appear')
    ps.add_argument('--json', action='store_true')
    ps.set_defaults(func=cmd_ps)

    for name, function in (('logs', cmd_logs), ('result', cmd_result), ('cancel', cmd_cancel)):
        node = sub.add_parser(name)
        node.add_argument('run')
        if name == 'result':
            node.add_argument('--json', action='store_true')
        node.set_defaults(func=function)

    doctor = sub.add_parser('doctor', help='check this shell and worktree; changes nothing')
    doctor.add_argument('--json', action='store_true')
    # What `doctor` asks of the `pandora` found on PATH, run from `/`: which
    # package did you import? Not for people.
    doctor.add_argument('--package-home', action='store_true', help=argparse.SUPPRESS)
    doctor.set_defaults(func=cmd_doctor)
    resolve = sub.add_parser('resolve', help='settle a conflicted --update write-back')
    resolve.add_argument('run')
    choice = resolve.add_mutually_exclusive_group(required=True)
    choice.add_argument('--keep-local', action='store_true',
                        help='the declared files as they are now are the answer')
    choice.add_argument('--take-worker', action='store_true',
                        help="replace them with the worker's proposed versions")
    resolve.set_defaults(func=cmd_resolve)

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
    if args.which in DEPRECATED:
        notice('`pandora %s` is deprecated; use `pandora %s`' % (args.which, DEPRECATED[args.which]))
    try:
        return args.func(args)
    except PandoraError as error:
        notice('%s: %s' % (type(error).__name__, error))
        return INFRA


if __name__ == '__main__':
    raise SystemExit(main())
