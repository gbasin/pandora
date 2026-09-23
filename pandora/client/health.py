"""Does the worker answer, and what did it say? Polled, cached, announced.

Three things this exists for, in the order they matter.

**Cost.** The slice measured a fallback against an unreachable worker at 12.2 s,
of which 10 s was the SSH connect timeout -- paid *per command*. An agent whose
worker died at lunchtime pays that on every `pnpm` it types for the rest of the
afternoon. A poll that already knows the worker is down turns that into an
immediate `fallback:worker-down`, and the only thing that costs is one SSH call
a minute that would otherwise never have been made.

**Attention.** A worker that goes away, a canary that starts failing, a pool
that crosses its floor or a kernel that changed under a reboot are all things
nobody is watching a terminal for. On this Mac that is a notification; anywhere
else it is a log line, because `osascript` is not a portable idea.

**Honesty about what a poll knows.** `reachable` means the engine answered and
said it was fit. `degraded` means it answered and said it was not -- a failed
canary, a pool below its floor, a drifted kernel -- which is a worker that may
still be perfectly able to run the job in front of it, so it is not a reason to
refuse anything. `down` means it did not answer. Only `down` changes what the
fallback path does, because only `down` is a fact about whether a submission can
succeed at all.

A state older than `stale_after` is treated as `unknown` rather than as its last
value: a daemon that has been asleep since yesterday knows nothing about now.
"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..errors import PandoraError

DEFAULT_INTERVAL = 60.0
# How long a reading stands for, in intervals: one or two missed polls are not a
# forgotten worker, three are.
STALE_FACTOR = 3.0
STATES = ('unknown', 'reachable', 'degraded', 'down')


def notify(title, message, *, enabled=True, platform=None, run=None):
    """One macOS notification. A no-op everywhere else, and never fatal.

    `osascript` quoting is the whole reason this is a function: the message is
    Pandora's own text, but it carries hostnames and reasons from the worker, so
    the quotes are escaped rather than trusted.
    """
    if not enabled:
        return False
    if (platform or sys.platform) != 'darwin':
        return False
    script = ('display notification "%s" with title "%s"'
              % (escape(message), escape(title)))
    runner = run or subprocess.run
    try:
        runner(['osascript', '-e', script], stdout=subprocess.DEVNULL,
               stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def escape(text):
    return str(text).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')[:300]


class Monitor:
    """The cached worker state, and the transitions worth telling someone about.

    `poll` is synchronous and does the work; `run` is the daemon's thread around
    it. Nothing here blocks a run: a command asks `state()`, which reads a
    dictionary, and the worst that a stale reading can do is cost one ordinary
    SSH timeout on the next submission.
    """

    def __init__(self, open_worker, *, interval=DEFAULT_INTERVAL, notify_enabled=True,
                 store=None, clock=time.time, log=None, notifier=notify):
        self.open_worker = open_worker
        self.interval = max(5.0, float(interval or DEFAULT_INTERVAL))
        self.notify_enabled = bool(notify_enabled)
        self.store = Path(store) if store else None
        self.clock = clock
        self.log = log or (lambda text: None)
        self.notifier = notifier
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.nudge = threading.Event()
        self.current = {'worker': 'unknown', 'at': 0, 'reason': None, 'canary': None,
                        'disk': None, 'goldens': 0, 'ready': None, 'kernel_drift': False,
                        'host': None, 'health': None, 'polls': 0}
        self._load()

    # -- reading -----------------------------------------------------------

    def state(self):
        """The cached picture, with `worker` degraded to `unknown` when stale."""
        with self.lock:
            item = dict(self.current)
        age = self.clock() - (item.get('at') or 0)
        item['age_seconds'] = round(age, 1)
        item['stale'] = item['worker'] != 'unknown' and age > self.interval * STALE_FACTOR
        if item['stale']:
            item['worker'] = 'unknown'
        return item

    def known_down(self):
        """The one question the submission path asks. Stale is not down."""
        return self.state()['worker'] == 'down'

    # -- polling -----------------------------------------------------------

    def poll(self):
        """One health call. Returns the new state; never raises."""
        try:
            worker = self.open_worker()
            answer = worker.health()
        except PandoraError as error:
            return self.record('down', reason=str(error)[:300])
        except Exception as error:                    # noqa: BLE001 - a probe, never a crash
            return self.record('down', reason='%s: %s' % (type(error).__name__, error))
        capacity = answer.get('capacity') or {}
        disk = ('below floor' if not capacity.get('ok')
                else ('%.1f GiB free' % capacity['free_gib'] if 'free_gib' in capacity
                      else 'unmeasured'))
        return self.record('reachable' if answer.get('ok') else 'degraded',
                           host=getattr(worker, 'host', None),
                           reason=answer.get('reason'),
                           canary=answer.get('canary'), disk=disk,
                           goldens=len(answer.get('goldens') or []),
                           ready=answer.get('state'),
                           kernel_drift=bool(answer.get('kernel_drift')),
                           health=answer)

    def record(self, worker, **fields):
        """Install a reading and announce whatever changed with it."""
        with self.lock:
            before = dict(self.current)
            self.current.update({'worker': worker, 'at': self.clock(),
                                 'polls': before.get('polls', 0) + 1})
            self.current.update({key: value for key, value in fields.items()
                                 if key in self.current})
            after = dict(self.current)
        for title, message in transitions(before, after):
            self.log('%s: %s' % (title, message))
            self.notifier(title, message, enabled=self.notify_enabled)
        self._save(after)
        return after

    def recheck(self):
        """Ask for a poll now rather than at the next tick.

        Called when something *else* just found the worker missing -- a submit
        that raised `WorkerUnreachable`, say. That is evidence the state is
        stale, but it is not itself a health verdict: a worker can refuse a
        submission and still be perfectly up, and only the health call is
        entitled to say `down`. So the finding triggers the question rather
        than answering it.
        """
        self.nudge.set()

    def run(self):
        """The daemon's thread: poll now, then every `interval` or on a nudge."""
        while not self.stopping.is_set():
            self.poll()
            self.nudge.clear()
            self.nudge.wait(self.interval)

    def start(self):
        thread = threading.Thread(target=self.run, daemon=True, name='pandora-health')
        thread.start()
        return thread

    def stop(self):
        self.stopping.set()
        self.nudge.set()

    # -- the cache on disk -------------------------------------------------

    def _load(self):
        if self.store is None or not self.store.is_file():
            return
        try:
            saved = json.loads(self.store.read_text())
        except (OSError, ValueError):
            return
        for key in self.current:
            if key in saved:
                self.current[key] = saved[key]

    def _save(self, item):
        if self.store is None:
            return
        try:
            self.store.write_text(json.dumps(item, sort_keys=True, default=str) + '\n')
        except OSError:
            pass                      # the cache is a convenience; never fail a run for it


def transitions(before, after):
    """[(title, message)] for what changed between two readings.

    Edges only, and only the five the brief names: the worker coming back, the
    worker going away, a canary that started failing, a pool that crossed its
    floor, a kernel that changed. A state that has not changed is not news, and
    a monitor that re-announces every minute is a monitor nobody reads.
    """
    out = []
    was, now = before.get('worker', 'unknown'), after.get('worker', 'unknown')
    if now == 'down' and was in ('reachable', 'degraded'):
        out.append(('pandora: worker down', after.get('reason') or 'the engine did not answer'))
    elif now in ('reachable', 'degraded') and was == 'down':
        out.append(('pandora: worker back', 'the engine answered; state %s' % now))
    if failed(after.get('canary')) and not failed(before.get('canary')):
        out.append(('pandora: worker canary failed',
                    'the worker last proved itself with %s failure(s)'
                    % (after['canary'] or {}).get('failures')))
    if after.get('disk') == 'below floor' and before.get('disk') != 'below floor':
        out.append(('pandora: worker disk floor',
                    'the pool is below its floor; new runs are being refused'))
    if after.get('kernel_drift') and not before.get('kernel_drift'):
        out.append(('pandora: worker kernel drift',
                    'the kernel changed since the canary passed; re-run '
                    '`pandora worker canary --mark`'))
    return out


def failed(canary):
    return bool(canary) and canary.get('ok') is False
