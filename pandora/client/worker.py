"""The real backend: freeze, ship, submit, stream, collect, report.

This is the half the POC faked. Every seam here is a real one, and the order is
load-bearing:

1. **classify** -- the repository's configuration says whether it claims this
   argv at all;
2. **validate** -- the repository's own runner says whether the arguments make
   sense, locally, in milliseconds, before anything is queued;
3. **freeze** -- the worktree becomes a manifest, and the manifest digest is the
   input id;
4. **ship** -- one rsync into the worker's per-repo source cache;
5. **submit** -- the engine admits it and names a run. A reply lost on the way
   back is not a refusal: the engine is asked by request id what it did
   (`Worker.recover`), and a worker that cannot be asked is `ExecutionUncertain`,
   which never falls back;
6. only now does the daemon say `accepted` to the client, because only now has
   the worker acknowledged anything. Everything above this line is provably
   non-executing, so a failure at any of those steps may still fall back to a
   local run. Everything below it may not.
7. **stream** -- the engine's log file, tailed by offset;
8. **collect** -- declared artifacts rsynced back into the worktree;
9. **result** -- the engine's result JSON, kept beside the run.

A run survives its client, its daemon and its SSH connection, because none of
them owns it: the engine's supervisor does, and the only thing that stops it is
`cancel`.
"""
import json
import os
import subprocess
import time
from pathlib import Path

from ..config import classify as classifier
from ..engine import bundle
from ..engine import writeback as engine_writeback
from ..errors import (EngineError, ExecutionUncertain, PandoraError, WorkerUnreachable)
from ..snapshot import freeze as snapshot
from ..snapshot import transfer
from . import writeback as writebacks

POLL_IDLE = 0.4
POLL_BUSY = 0.15


class Submission:
    """What the worker acknowledged, and what it took to get there."""

    def __init__(self, run_id, *, admission=None, duplicate=False, same_tree_as=None,
                 input_id='', durations=None, source=None, shipped=(), writeback=None):
        self.run_id = run_id
        self.admission = admission or {}
        self.duplicate = duplicate
        # The engine's `same_input_as`: the previous attempt over the same tree
        # digest, whatever its argv. Renamed on this side only; the engine's
        # wire key and ledger column keep the old name.
        self.same_tree_as = same_tree_as
        self.input_id = input_id
        self.durations = durations or {}
        self.source = source or {}
        # The frozen manifest's paths. Kept for the gitignored-path hint, which
        # needs to know what was *not* shipped; ~5,000 strings, held per run.
        self.shipped = frozenset(shipped)
        # For a write-back run, what the declared files and the rest of the tree
        # were at freeze time (`client.writeback.context`). None otherwise.
        self.writeback = writeback


class Worker:
    """One worker, one SSH conversation, for the life of the daemon."""

    def __init__(self, host, *, state, engine_root='pandora-engine', persist='10m',
                 source_root=None, client=None):
        if not host:
            raise WorkerUnreachable('no worker host is configured')
        self.host = host
        # Who this daemon is to the engine: `user@host`, or `[client] name`.
        # Sent with every submission and every cancel; the daemon keeps it current.
        self.client = client
        self.engine_root = engine_root
        self.state = Path(state)
        self.link = transfer.Link(host, self.state / 'ssh', persist=persist)
        self.source_root = source_root
        self._bundle = None
        self._root = engine_root if engine_root.startswith('/') else None

    def root(self):
        """The engine root as an absolute path on the worker.

        A relative root is resolved against the worker's home directory, once.
        It has to be absolute before anything uses it, because the engine runs
        with its working directory inside the shipped bundle and rsync resolves
        a relative destination against the login directory -- two different
        answers for one configured string, which is how an engine ends up
        writing its ledger inside its own bundle.
        """
        if self._root is None:
            _, out, _ = self.link.run(['sh', '-c', 'cd "$HOME" && pwd'], timeout=60)
            self._root = out.strip().rstrip('/') + '/' + self.engine_root.lstrip('./')
        return self._root

    def bundle_path(self):
        if self._bundle is None:
            self._bundle = bundle.ensure(self.link, self.root(),
                                         source_root=self.source_root)
        return self._bundle['path']

    def engine(self, argv, **kwargs):
        return bundle.call(self.link, self.bundle_path(), self.root(), argv, **kwargs)

    # -- submission --------------------------------------------------------

    def submit(self, *, plan, worktree, request_id, cache_root=None, control=None,
               progress=None, phase=None, log=None, transfer_stderr=None):
        """Freeze, ship and submit. Raises before the worker acknowledges anything.

        `progress` receives one line when a transfer actually starts, naming the
        size of the tree being synced. It is the tree's size, not the bytes on
        the wire: rsync with `--link-dest` sends only what the cache lacks, and
        which that is is not known until it has finished.

        `phase` is called with `freeze`, `ship` and `submit` as each begins, so
        a row can say where a slow submission is. An exception raised from here
        carries `pre_accept`: the timings so far, the failing step's included.

        `log` gets the transfer's start, end or failure as one line each, for
        the daemon's log; `transfer_stderr` is where rsync's stderr is kept.
        """
        marks = {}
        step = {'name': None, 'at': time.monotonic()}

        def enter(name):
            step['name'], step['at'] = name, time.monotonic()
            if phase is not None:
                phase(name)

        def leave():
            marks[step['name']] = round(time.monotonic() - step['at'], 2)
            step['name'] = None

        try:
            enter('freeze')
            manifest, dropped, input_id = snapshot.freeze(
                worktree, exclude_globs=plan['secrets_exclude_globs'],
                cache=getattr(self, 'state', None) and self.state / 'digests')
            leave()

            enter('ship')
            shipping = {}

            def on_send():
                # One stat per file, once, and only on a cache miss.
                shipping['line'] = sync_line(worktree, manifest)
                shipping['at'] = time.monotonic()
                if log is not None:
                    log('transfer start: input %s, %s, from %s'
                        % (input_id, shipping['line'][len('syncing '):], worktree))
                if progress is not None:
                    progress(shipping['line'])
            try:
                source = transfer.send(
                    self.link, manifest, worktree=worktree, root=cache_root or self.root(),
                    repo=plan['repo'], input_id=input_id, on_send=on_send,
                    log=log, stderr_path=transfer_stderr)
            except PandoraError as error:
                if log is not None:
                    log('transfer failed: input %s, %s, rsync exit %s, after %.1f s: %s'
                        % (input_id, shipping.get('line', 'before rsync started'),
                           getattr(error, 'rsync_exit', '-'),
                           time.monotonic() - shipping.get('at', step['at']), error))
                raise
            if log is not None and shipping:
                log('transfer done: input %s, %s, rsync exit 0, %.1f s'
                    % (input_id, shipping['line'][len('syncing '):],
                       time.monotonic() - shipping['at']))
            leave()

            enter('submit')
            request = {'request_id': request_id, 'input_id': input_id,
                       'source_path': source['path'], 'plan': plan,
                       'manifest_files': len(manifest), 'dropped': len(dropped)}
            if self.client:
                request['client'] = self.client
            if plan.get('git') == 'synthetic':
                # The engine builds the run's repository from the tree plus these
                # two lists, so its index is this worktree's tracked set.
                request['git_marks'] = snapshot.git_marks(manifest)
            # How many shards the caller asked for and whether a failing shard
            # stops the rest. Decisions about *this invocation*, not about the
            # repository, so they travel beside the plan rather than inside it.
            request.update({key: value for key, value in (control or {}).items()
                            if key in ('want_shards', 'keep_going')})
            # Resolved first, so a bundle that cannot be placed fails as what it
            # is: before the engine has seen this request, and still a fallback.
            self.bundle_path()
            try:
                answer = self.engine(['submit'], stdin=json.dumps(request), timeout=120)
            except (WorkerUnreachable, EngineError, subprocess.TimeoutExpired) as error:
                # The call itself failed, which is not the same as the engine
                # refusing. It may have claimed and spawned the run and lost only
                # the reply, so the answer is asked for again rather than assumed.
                answer = self.recover(request_id, plan, error)
            leave()
        except Exception as error:
            if step['name'] is not None:
                marks[step['name']] = round(time.monotonic() - step['at'], 2)
            try:
                error.pre_accept = dict(marks)
            except AttributeError:
                pass
            raise
        if not answer.get('ok'):
            error = EngineError(json.dumps({'code': answer.get('code', 'rejected'),
                                            'detail': answer.get('admission')}))
            error.pre_accept = dict(marks)
            raise error
        return Submission(answer['run_id'], admission=answer.get('admission'),
                          duplicate=answer.get('duplicate', False),
                          same_tree_as=answer.get('same_input_as'),
                          input_id=input_id, durations=marks, source=source,
                          shipped=(record['path'] for record in manifest),
                          writeback=(writebacks.context(manifest, plan, worktree=worktree,
                                                        input_id=input_id)
                                     if plan.get('writeback') else None))

    def lookup(self, request_id, *, plan=None, fence=True):
        """The engine's record of one request id (`service.cmd_lookup`)."""
        argv = ['lookup', '--request-id', request_id] + self.as_client()
        if fence:
            argv += ['--fence', '--repo', (plan or {}).get('repo') or '',
                     '--job', (plan or {}).get('job') or '']
        return self.engine(argv, timeout=60)

    def recover(self, request_id, plan, error):
        """Turn a failed `submit` call into a `submit` answer, or say it cannot.

        Asked once, by the request id `submit` is idempotent on:

        * the run was spawned -- the answer `submit` would have given, marked
          `duplicate`, so the caller attaches exactly as if it had arrived;
        * it was refused before anything ran -- that refusal, with its cause, so
          the caller's fallback policy decides as it would have;
        * the engine has no such request -- the original error, re-raised, and a
          fallback is permitted as before. The lookup fenced the id, so a submit
          still in flight on the worker cannot start it afterward;
        * the engine cannot be asked, or answers anything else --
          `ExecutionUncertain`. Never a fallback: the command may be running.
        """
        try:
            found = self.lookup(request_id, plan=plan)
        except (PandoraError, subprocess.TimeoutExpired, OSError) as again:
            raise ExecutionUncertain(
                'submit failed (%s) and the engine could not be asked whether it started '
                'the run (%s)' % (error, again)) from error
        if not found.get('ok'):
            raise ExecutionUncertain('submit failed (%s); the engine answered the lookup '
                                     'with %s' % (error, found)) from error
        if not found.get('found'):
            if isinstance(error, PandoraError):
                raise error
            raise WorkerUnreachable('submit timed out after %ss' % error.timeout) from error
        if found.get('spawned'):
            return {'ok': True, 'duplicate': True, 'run_id': found['run_id'],
                    'state': found.get('state'), 'same_input_as': found.get('same_input_as'),
                    'admission': found.get('admission') or {}, 'recovered': str(error)}
        if found.get('state') == 'finished':
            return {'ok': False, 'code': found.get('cause') or 'rejected',
                    'admission': found.get('admission')}
        # Claimed, not finished, and no supervisor recorded: `submit` died between
        # the claim and the pid write, and whether `runner.spawn` ran is unknown.
        raise ExecutionUncertain('submit failed (%s) with run %s claimed but not recorded '
                                 'as started' % (error, found.get('run_id'))) from error

    def resubmit(self, run_id, *, request_id):
        """One more attempt at a finished `infra_failed` run, from the same input.

        Nothing is frozen or shipped: the engine reads the first attempt's own
        request back, so the retry tests the tree the caller was told about.
        Raises `EngineError` when the engine refuses, exactly as `submit` does.
        """
        answer = self.engine(['resubmit', '--run', run_id, '--request-id', request_id]
                             + self.as_client(), timeout=120)
        if not answer.get('ok'):
            raise EngineError(json.dumps({'code': answer.get('code', 'rejected'),
                                          'detail': answer.get('admission')}))
        return Submission(answer['run_id'], admission=answer.get('admission'),
                          duplicate=answer.get('duplicate', False),
                          same_tree_as=answer.get('same_input_as'))

    # -- following a run ---------------------------------------------------

    def status(self, run_id):
        return self.engine(['status', '--run', run_id], timeout=60)

    def logs(self, run_id, offset):
        return self.engine(['logs', '--run', run_id, '--offset', str(offset)],
                           timeout=120, binary=True, check=False)

    def result(self, run_id):
        return self.engine(['result', '--run', run_id], timeout=60)

    def cancel(self, run_id):
        return self.engine(['cancel', '--run', run_id] + self.as_client(), timeout=60)

    def as_client(self):
        """`--client NAME` for an engine verb scoped to this client, or nothing."""
        return ['--client', self.client] if self.client else []

    def ps(self, *, live=False, limit=25):
        return self.engine(['ps'] + (['--live'] if live else []) + ['--limit', str(limit)],
                           timeout=60)

    def stats(self):
        return self.engine(['stats'], timeout=60)

    def health(self, *, timeout=20):
        """The cheap poll: one engine call over the ControlMaster.

        The timeout is short on purpose. The whole point of polling is that the
        *next* command does not pay a 12-second SSH connect to discover a worker
        that has been gone for ten minutes, so the poll that discovers it must
        not sit for twelve seconds either.
        """
        return self.engine(['health'], timeout=timeout)

    def reconcile(self):
        return self.engine(['reconcile'], timeout=300)

    def follow(self, run_id, *, on_log, offset=0, should_cancel=None, deadline=None,
               on_status=None):
        """Tail one run to its verdict. Returns (result, offset).

        Slow-polled on purpose: the engine writes the log to a file and this
        copies byte ranges of it, so a dropped connection costs an offset and
        nothing else. `should_cancel` is checked on the same beat, so a Ctrl-C
        reaches the worker within one poll. `on_status` sees every status row,
        which is how the daemon knows a run's phase without asking again.
        """
        canceled = False
        while True:
            if should_cancel is not None and should_cancel() and not canceled:
                self.cancel(run_id)
                canceled = True
            chunk = self.logs(run_id, offset)
            if chunk:
                on_log(chunk)
                offset += len(chunk)
            row = self.status(run_id)
            if not row.get('ok'):
                raise EngineError('the engine no longer knows run %s' % run_id)
            if on_status is not None:
                on_status(row)
            if row['state'] == 'finished':
                tail = self.logs(run_id, offset)
                if tail:
                    on_log(tail)
                    offset += len(tail)
                answer = self.result(run_id)
                if not answer.get('ok'):
                    raise EngineError('run %s finished with no result' % run_id)
                return answer['result'], offset
            if deadline is not None and time.monotonic() > deadline:
                return None, offset
            time.sleep(POLL_BUSY if chunk else POLL_IDLE)

    def collect(self, run_id, plan, *, worktree):
        """Bring declared artifacts back to their worktree-relative locations."""
        paths = [path for output in plan['outputs'] if output['kind'] == 'artifacts'
                 for path in output['paths']]
        if not paths:
            return {'paths': [], 'fetched': False}
        remote = '%s/runs/%s/outputs' % (self.root(), run_id)
        transfer.fetch(self.link, remote, worktree, timeout=900)
        present = [path for path in paths if (Path(worktree) / path).exists()]
        return {'paths': paths, 'present': present,
                'missing': [path for path in paths if path not in present], 'fetched': True}

    def fetch_writeback(self, run_id, into):
        """Bring a run's write-back proposal into `into`, never into the worktree.

        A separate directory from the artifacts on both ends: artifacts are
        rsynced straight into the worktree, and a proposal must not reach it
        before `client.writeback.settle` has said it may.
        """
        remote = '%s/runs/%s/%s' % (self.root(), run_id, engine_writeback.PROPOSAL)
        transfer.fetch(self.link, remote, into, timeout=900)

    def close(self):
        self.link.close()


def sync_line(worktree, manifest):
    """`syncing 4,912 files, 375 MiB`: one stat per file, only on a cache miss."""
    total = 0
    root = Path(worktree)
    for record in manifest:
        if 'link' in record:
            continue
        try:
            total += (root / record['path']).stat().st_size
        except OSError:
            continue
    return 'syncing {:,} files, {}'.format(len(manifest), size_text(total))


def size_text(count):
    if count >= 1 << 30:
        return '%.1f GiB' % (count / float(1 << 30))
    if count >= 1 << 20:
        return '%d MiB' % round(count / float(1 << 20))
    return '%d KiB' % max(1, round(count / 1024.0))


def preflight(config, plan, job, *, worktree, forwarded, env):
    """Run the repository's own validator, in the worktree, before submitting."""
    passthrough = {name: env[name] for name in plan['env_passthrough'] if name in env}
    base = {'PATH': os.environ.get('PATH', ''), 'HOME': os.environ.get('HOME', ''),
            # The shim must not re-enter itself out of its own pre-flight check.
            'PANDORA_ROUTE_DEPTH': '1', 'PANDORA_VALIDATE': '1'}
    base.update(passthrough)
    return classifier.preflight(job, forwarded, root=worktree, extra_env=base)


def announce(text, stream=None):
    """Every line Pandora writes about itself goes to stderr, prefixed."""
    import sys
    handle = stream or sys.stderr
    handle.write('pandora: ' + text + '\n')
    handle.flush()
