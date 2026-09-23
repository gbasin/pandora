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
5. **submit** -- the engine admits it and names a run;
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
import time
from pathlib import Path

from ..config import classify as classifier
from ..engine import bundle
from ..engine import writeback as engine_writeback
from ..errors import (EngineError, PandoraError, TransferError, WorkerUnreachable)
from ..snapshot import freeze as snapshot
from ..snapshot import transfer
from . import writeback as writebacks

POLL_IDLE = 0.4
POLL_BUSY = 0.15


class Submission:
    """What the worker acknowledged, and what it took to get there."""

    def __init__(self, run_id, *, admission=None, duplicate=False, same_input_as=None,
                 input_id='', durations=None, source=None, shipped=(), writeback=None):
        self.run_id = run_id
        self.admission = admission or {}
        self.duplicate = duplicate
        self.same_input_as = same_input_as
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
                 source_root=None):
        if not host:
            raise WorkerUnreachable('no worker host is configured')
        self.host = host
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

    def submit(self, *, plan, worktree, request_id, cache_root=None, control=None):
        """Freeze, ship and submit. Raises before the worker acknowledges anything."""
        marks = {}
        started = time.monotonic()
        manifest, dropped, input_id = snapshot.freeze(
            worktree, exclude_globs=plan['secrets_exclude_globs'],
            cache=getattr(self, 'state', None) and self.state / 'digests')
        marks['freeze'] = round(time.monotonic() - started, 2)

        mark = time.monotonic()
        source = transfer.send(self.link, manifest, worktree=worktree,
                               root=cache_root or self.root(),
                               repo=plan['repo'], input_id=input_id)
        marks['ship'] = round(time.monotonic() - mark, 2)

        mark = time.monotonic()
        request = {'request_id': request_id, 'input_id': input_id,
                   'source_path': source['path'], 'plan': plan,
                   'manifest_files': len(manifest), 'dropped': len(dropped)}
        if plan.get('git') == 'synthetic':
            # The engine builds the run's repository from the tree plus these
            # two lists, so its index is this worktree's tracked set.
            request['git_marks'] = snapshot.git_marks(manifest)
        # How many shards the caller asked for and whether a failing shard stops
        # the rest. Decisions about *this invocation*, not about the repository,
        # so they travel beside the plan rather than inside it.
        request.update({key: value for key, value in (control or {}).items()
                        if key in ('want_shards', 'keep_going')})
        answer = self.engine(['submit'], stdin=json.dumps(request), timeout=120)
        marks['submit'] = round(time.monotonic() - mark, 2)
        if not answer.get('ok'):
            raise EngineError(json.dumps({'code': answer.get('code', 'rejected'),
                                          'detail': answer.get('admission')}))
        return Submission(answer['run_id'], admission=answer.get('admission'),
                          duplicate=answer.get('duplicate', False),
                          same_input_as=answer.get('same_input_as'),
                          input_id=input_id, durations=marks, source=source,
                          shipped=(record['path'] for record in manifest),
                          writeback=(writebacks.context(manifest, plan, worktree=worktree,
                                                        input_id=input_id)
                                     if plan.get('writeback') else None))

    # -- following a run ---------------------------------------------------

    def status(self, run_id):
        return self.engine(['status', '--run', run_id], timeout=60)

    def logs(self, run_id, offset):
        return self.engine(['logs', '--run', run_id, '--offset', str(offset)],
                           timeout=120, binary=True, check=False)

    def result(self, run_id):
        return self.engine(['result', '--run', run_id], timeout=60)

    def cancel(self, run_id):
        return self.engine(['cancel', '--run', run_id], timeout=60)

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

    def follow(self, run_id, *, on_log, offset=0, should_cancel=None, deadline=None):
        """Tail one run to its verdict. Returns (result, offset).

        Slow-polled on purpose: the engine writes the log to a file and this
        copies byte ranges of it, so a dropped connection costs an offset and
        nothing else. `should_cancel` is checked on the same beat, so a Ctrl-C
        reaches the worker within one poll.
        """
        cancelled = False
        while True:
            if should_cancel is not None and should_cancel() and not cancelled:
                self.cancel(run_id)
                cancelled = True
            chunk = self.logs(run_id, offset)
            if chunk:
                on_log(chunk)
                offset += len(chunk)
            row = self.status(run_id)
            if not row.get('ok'):
                raise EngineError('the engine no longer knows run %s' % run_id)
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
