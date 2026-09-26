"""A batch queue as a directory, shared by a fan-out's parent and its shards.

`strategy = "queue"` replaces the static partition with a pull model: the plan
step still emits the inventory once, the parent flattens it into batch spec
files under `<parent attempt>/queue/`, and each shard's supervisor -- an
ordinary child run on the same host -- claims them first-free-first-served:

    pending/<seq>.json    {"batch": seq, "testIds": [...]}   not yet claimed
    leased/<seq>.json     + {"holder": run_id, "attempt": n} a shard holds it
    done/<seq>.json       + {"holder", "outcome", "report"}  it ran
    dead/<seq>.json       attempts exhausted; ids never observed
    attempts/<seq>        one int: how many times it was claimed
    halt                  exists: stop claiming new batches

The claim is an atomic rename; the rest of the protocol runs under a flock on
`lock`, so a crashed writer can leave at most a leased file that does not name
its holder -- which the parent requeues like any other orphaned lease.

A shard that dies holding a batch leaves the lease. The parent's poll loop
returns it to `pending` while its attempt count is under `batch_attempts`,
then moves it to `dead`, where verification names the ids that never ran. A
poison batch therefore costs its cap times a batch, never the run.
"""
import fcntl
import json
from contextlib import contextmanager
from pathlib import Path

SUBS = ('pending', 'leased', 'done', 'dead', 'attempts')


class Queue:
    def __init__(self, directory):
        self.dir = Path(directory)

    def seed(self, batches):
        """Parent-side, once: (seq, ids) rows become pending spec files."""
        for name in SUBS:
            (self.dir / name).mkdir(parents=True, exist_ok=True)
        for seq, ids in batches:
            (self.dir / 'pending' / ('%06d.json' % seq)).write_text(
                json.dumps({'batch': seq, 'testIds': ids}))
        return self

    @contextmanager
    def _lock(self):
        (self.dir / 'lock').touch(exist_ok=True)
        with (self.dir / 'lock').open('r') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    # -- the shard half ------------------------------------------------------

    def claim(self, holder):
        """The next batch's spec for this shard, or None when it should stop.

        None means either the queue is drained or halted; the holder does not
        need to know which -- its own work is finished either way.
        """
        with self._lock():
            if (self.dir / 'halt').exists():
                return None
            pending = sorted((self.dir / 'pending').iterdir())
            if not pending:
                return None
            spec = pending[0]
            data = json.loads(spec.read_text())
            seq = data['batch']
            mark = self.dir / 'attempts' / ('%06d' % seq)
            attempt = int(mark.read_text() or 0) + 1 if mark.exists() else 1
            mark.write_text(str(attempt))
            data.update({'holder': holder, 'attempt': attempt})
            leased = self.dir / 'leased' / spec.name
            spec.rename(leased)
            leased.write_text(json.dumps(data))
            return data

    def complete(self, seq, holder, *, outcome, report):
        """The holder is done with this batch, pass or fail: leased -> done."""
        with self._lock():
            leased = self.dir / 'leased' / ('%06d.json' % seq)
            data = json.loads(leased.read_text()) if leased.exists() else {'batch': seq}
            data.update({'holder': holder, 'outcome': outcome, 'report': report})
            done = self.dir / 'done' / ('%06d.json' % seq)
            done.write_text(json.dumps(data))
            leased.unlink(missing_ok=True)

    # -- the parent half -----------------------------------------------------

    def release_dead(self, finished, *, cap):
        """Leases whose holder finished: back to `pending`, or to `dead`.

        `finished` answers whether a run id's row is done for good. Returns
        (requeued, dead) batch numbers so the caller can say which.
        """
        requeued, dead = [], []
        with self._lock():
            for lease in sorted((self.dir / 'leased').iterdir()):
                data = json.loads(lease.read_text())
                holder = data.get('holder')
                if holder is not None and not finished(holder):
                    continue
                seq = data['batch']
                mark = self.dir / 'attempts' / ('%06d' % seq)
                attempts = int(mark.read_text() or 0) if mark.exists() else 0
                if attempts < cap:
                    target = self.dir / 'pending' / lease.name
                    target.write_text(json.dumps({'batch': seq,
                                                  'testIds': data.get('testIds', [])}))
                    lease.unlink()
                    requeued.append(seq)
                else:
                    data['attempts'] = attempts
                    lease.rename(self.dir / 'dead' / lease.name)
                    dead.append(seq)
        return requeued, dead

    def halt(self):
        with self._lock():
            (self.dir / 'halt').touch(exist_ok=True)

    def halted(self):
        return (self.dir / 'halt').exists()

    def failed(self):
        """Any done batch whose run failed -- what `keep_going` gates on."""
        out = []
        for record in sorted((self.dir / 'done').iterdir()):
            data = json.loads(record.read_text())
            if data.get('outcome') != 'ok':
                out.append(data)
        return out

    def drained(self):
        """Nothing left to claim and nobody holding one."""
        return (not list((self.dir / 'pending').iterdir())
                and not list((self.dir / 'leased').iterdir()))

    def snapshot(self):
        """{pending, leased, done, dead} seq lists -- for evidence, cheaply."""
        return {name: sorted(int(path.stem)
                             for path in (self.dir / name).iterdir()
                             if path.suffix == '.json')
                for name in ('pending', 'leased', 'done', 'dead')}
