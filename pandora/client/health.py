"""Does the worker answer, and what did it say? Polled, cached, announced.

Three things this exists for, in the order they matter.

**Cost.** The slice measured a fallback against an unreachable worker at 12.2 s,
of which 10 s was the SSH connect timeout -- paid *per command*. An agent whose
worker died at lunchtime pays that on every `pnpm` it types for the rest of the
afternoon. A poll that already knows the worker is down turns that into an
immediate `fallback:worker-down`, and the only thing that costs is one SSH call
a minute that would otherwise never have been made.

**Attention.** A worker that goes away or turns degraded, a canary that starts
failing, a pool that nears or crosses its floor or a kernel that changed under
a reboot are all things nobody is watching a terminal for. On this Mac that is a notification; anywhere
else it is a log line, because `osascript` is not a portable idea.

**Honesty about what a poll knows.** `reachable` means the engine answered and
said it was fit. `degraded` means it answered and said it was not -- a failed
canary, a pool below its floor, a drifted kernel -- which is a worker that may
still be perfectly able to run the job in front of it, so it is not a reason to
refuse anything. `down` means two consecutive probes failed to answer. Only
`down` changes what the fallback path does. One missed probe keeps the last
reading without refreshing its timestamp.

After a restart, cached facts remain available for diagnostics and transition
notifications. A cached `down` result reads as `unknown` until this daemon has
two failed probes: the old daemon's failure must not route new requests locally.
A state older than `stale_after` also reads as `unknown`.

**Saying why a probe failed.** A failed probe, and a probe slower than
`SLOW_PROBE_SECONDS`, logs one line: how long it took, which stage failed
(`transport` for ssh itself, `remote` for the engine's health command,
`timeout` for a client-side timeout with no output), the ssh exit code and the
last stderr line, whether the ssh control master answers, and what this host's
memory and load looked like at that moment. Those are the four suspects for a
probe that times out: the connection, the multiplexing socket, the remote
command, or a Mac too busy to run ssh promptly. The line comes after the
verdict, never before it, and nothing that builds it can stop the polling. A
failed probe logs it on the first failure and on the down transition, then at
most every `DIAGNOSE_EVERY_SECONDS` while the worker stays down; a slow probe at
most that often. The answer that ends a run of failures logs one plain line.
"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..errors import EngineError, PandoraError, WorkerUnreachable
from . import pressure

DEFAULT_INTERVAL = 60.0
# How long a reading stands for, in intervals: one or two missed polls are not a
# forgotten worker, three are.
STALE_FACTOR = 3.0
# The soft disk threshold, as a multiple of the admission floor: inside it the
# pool still takes runs, but the floor is close enough to say so once.
DISK_WARN_FACTOR = 1.5
STATES = ('unknown', 'reachable', 'degraded', 'down')
# A probe that answers but takes this long still gets a diagnostic line.
SLOW_PROBE_SECONDS = 5.0
# The failure diagnostics' own bounds: neither may hold up the next poll for long.
MUX_CHECK_SECONDS = 3.0
# While the worker stays down, the full diagnostic repeats at most this often.
DIAGNOSE_EVERY_SECONDS = 600.0
PRESSURE_WAIT_SECONDS = 2.0
# ssh's own stderr, mapped to the transport failure it names.
SSH_HINTS = (
    (('timed out', 'timeout'), 'connect timeout'),
    (('permission denied', 'host key verification', 'authentication'), 'auth'),
    (('mux_client', 'control socket', 'controlsocket', 'controlpath', 'master'), 'mux'),
    (('could not resolve',), 'dns'),
    (('connection refused',), 'refused'),
    (('connection closed', 'connection reset', 'broken pipe'), 'connection lost'),
    (('no route',), 'no route'),
)


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
                 store=None, clock=time.time, log=None, notifier=notify, gate=None,
                 timer=time.monotonic):
        self.open_worker = open_worker
        self.interval = max(5.0, float(interval or DEFAULT_INTERVAL))
        self.notify_enabled = bool(notify_enabled)
        self.store = Path(store) if store else None
        self.clock = clock
        self.log = log or (lambda text: None)
        self.notifier = notifier
        # The local pressure gate, read (never sampled) for a failed probe's line.
        self.gate = gate
        self.timer = timer
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.nudge = threading.Event()
        self.current = {'worker': 'unknown', 'at': 0, 'reason': None, 'canary': None,
                        'disk': None, 'free_gib': None, 'floor_gib': None, 'goldens': 0,
                        'ready': None, 'kernel_drift': False,
                        'host': None, 'health': None, 'polls': 0, 'probe': None}
        self.failures = 0
        # When this monitor last logged a failed or a slow probe, on `timer`.
        self.failed_logged_at = None
        self.slow_logged_at = None
        self._load()

    # -- reading -----------------------------------------------------------

    def state(self):
        """The cached picture; stale or unconfirmed down readings are unknown."""
        with self.lock:
            item = dict(self.current)
        age = self.clock() - (item.get('at') or 0)
        item['age_seconds'] = round(age, 1)
        item['stale'] = item['worker'] != 'unknown' and age > self.interval * STALE_FACTOR
        if item['stale'] or (item['worker'] == 'down' and self.failures < 2):
            item['worker'] = 'unknown'
        return item

    def known_down(self):
        """The one question the submission path asks. Stale is not down."""
        return self.state()['worker'] == 'down'

    # -- polling -----------------------------------------------------------

    def poll(self):
        """One health call. Returns the new state; never raises."""
        worker = None
        started = self.timer()
        try:
            worker = self.open_worker()
            answer = worker.health()
        except Exception as error:                    # noqa: BLE001 - a probe, never a crash
            ended = self.timer()
            seconds = ended - started
            reason = (str(error)[:300] if isinstance(error, PandoraError)
                      else '%s: %s' % (type(error).__name__, error))
            try:
                found = classify(error)
            except Exception:                         # noqa: BLE001 - diagnostics never decide
                found = None
            if found is not None and not isinstance(error, PandoraError):
                reason = '%s: %s' % (found['stage'], found['detail'])
            # The verdict first: diagnostics must not delay a down transition.
            state = self.failed(reason, probe=self.probe_record(
                seconds, found or {'stage': 'error'}))
            if self.failures <= 2 or self.due(self.failed_logged_at, ended):
                self.failed_logged_at = ended
                self.report(worker, seconds, found, reason)
            return state
        ended = self.timer()
        seconds = ended - started
        if self.failures:
            self.log('worker health probe answered after %.1f s, after %d consecutive '
                     'failure(s)' % (seconds, self.failures))
        elif seconds >= SLOW_PROBE_SECONDS and self.due(self.slow_logged_at, ended):
            self.slow_logged_at = ended
            self.report(worker, seconds, None, None)
        self.failures = 0
        capacity = answer.get('capacity') or {}
        disk = ('below floor' if not capacity.get('ok')
                else ('%.1f GiB free' % capacity['free_gib'] if 'free_gib' in capacity
                      else 'unmeasured'))
        return self.record('reachable' if answer.get('ok') else 'degraded',
                           host=getattr(worker, 'host', None),
                           reason=answer.get('reason'),
                           canary=answer.get('canary'), disk=disk,
                           free_gib=capacity.get('free_gib'),
                           floor_gib=capacity.get('floor_gib'),
                           goldens=len(answer.get('goldens') or []),
                           ready=answer.get('state'),
                           kernel_drift=bool(answer.get('kernel_drift')),
                           health=answer, probe=self.probe_record(seconds, None))

    def probe_record(self, seconds, found):
        """The last probe's latency and outcome, kept in worker-health.json."""
        return {'seconds': round(seconds, 2), 'at': self.clock(), 'ok': found is None,
                'stage': found['stage'] if found else None}

    @staticmethod
    def due(last, now):
        return last is None or now - last >= DIAGNOSE_EVERY_SECONDS

    def report(self, worker, seconds, found, reason):
        """Log the diagnostic line; never let building it break the probe loop."""
        try:
            line = self.diagnose(worker, seconds, found)
        except Exception as error:                    # noqa: BLE001 - diagnostics never decide
            line = ('worker health probe %s after %.1f s (consecutive failure %d): %s '
                    '[diagnostics failed: %s]'
                    % ('failed' if reason else 'slow', seconds, self.failures,
                       reason or 'answered', type(error).__name__))
        self.log(line)

    def diagnose(self, worker, seconds, found):
        """One compact log line for a failed or slow probe.

        Read after the probe returned, never during it: the ssh control
        master's own answer and this host's pressure are both bounded, so a
        wedged socket or a starved Mac shows up as such rather than as a
        hung health thread.
        """
        if found is None:
            head = 'worker health probe slow: answered after %.1f s' % seconds
        else:
            head = ('worker health probe failed after %.1f s (consecutive failure %d): %s'
                    % (seconds, self.failures, describe(found)))
        parts = [head]
        mux = mux_state(worker)
        if mux:
            parts.append('ssh mux: ' + mux)
        parts.append('host: ' + host_pressure(self.gate))
        return '; '.join(parts)

    def failed(self, reason, *, probe=None):
        """One missed poll does not make a busy worker unavailable.

        Keep the previous reading and its timestamp on the first failure. It
        still ages normally; a failed probe is not fresh evidence of health.
        Any answer resets the count, including an explicitly degraded answer.
        """
        self.failures += 1
        if self.failures < 2:
            if probe is not None:
                with self.lock:
                    self.current['probe'] = probe
                    item = dict(self.current)
                self._save(item)
            return self.state()
        return self.record('down', reason=reason, probe=probe)

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
            try:
                self.poll()
            except Exception as error:                # noqa: BLE001 - the thread must outlive a poll
                self.log('worker health poll raised %s: %s; polling continues'
                         % (type(error).__name__, error))
            if self.stopping.is_set():
                # Checked again after the poll: `stop` sets `nudge` to break the
                # wait, and a clear here would eat that signal and park the
                # thread for the whole interval past the daemon's stop.
                return
            self.nudge.clear()
            self.nudge.wait(self.interval)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name='pandora-health')
        self.thread.start()
        return self.thread

    def stop(self):
        self.stopping.set()
        self.nudge.set()
        # Wait for a poll in flight: its record() writes the cache file, and a
        # caller that tears `store`'s directory down right after `stop` --
        # the test suite's temporary state directory -- must not race it.
        thread = getattr(self, 'thread', None)
        if thread is not None:
            thread.join(timeout=5.0)

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


def classify(error):
    """{stage, detail, exit, stderr, hint} for one failed probe.

    `transport` is ssh itself (exit 255: connect, auth, the control master).
    `remote` is the engine's health command: a nonzero exit, no output, output
    that is not JSON, or a command still printing when the timeout hit.
    `timeout` is the client giving up with nothing on stdout, which leaves the
    connection and the remote command both suspects; the mux and host readings
    in the same line are what tell them apart. `error` is anything else.
    """
    if isinstance(error, subprocess.TimeoutExpired):
        out = error.stdout or b''
        stderr = last_line(error.stderr)
        if out.strip():
            return {'stage': 'remote', 'exit': None, 'stderr': stderr, 'hint': None,
                    'detail': 'still running at the %g s timeout after %d bytes of output'
                              % (error.timeout, len(out))}
        return {'stage': 'timeout', 'exit': None, 'stderr': stderr, 'hint': None,
                'detail': 'no output within %g s' % error.timeout}
    code = getattr(error, 'returncode', None)
    stderr = last_line(getattr(error, 'stderr', None))
    message = str(error)
    if isinstance(error, WorkerUnreachable):
        if code is None and 'no answer within' in message:
            # `Link._ssh`'s timeout, on the first poll's root lookup.
            return {'stage': 'timeout', 'exit': None, 'stderr': None, 'hint': None,
                    'detail': message}
        stderr = stderr or last_line(message.partition(': ')[2]) or None
        return {'stage': 'transport', 'exit': code, 'stderr': stderr,
                'hint': ssh_hint(stderr),
                'detail': 'ssh exit %d' % code if code else 'unreachable'}
    if isinstance(error, EngineError):
        if code:
            detail = 'exit %d' % code
        elif 'said nothing' in message:
            detail = 'no output'
        elif 'non-JSON' in message:
            detail = 'bad JSON'
        else:
            detail = message[:160]
        return {'stage': 'remote', 'exit': code, 'stderr': stderr, 'hint': None,
                'detail': detail}
    return {'stage': 'error', 'exit': code, 'stderr': stderr, 'hint': None,
            'detail': ('%s: %s' % (type(error).__name__, message))[:160]}


def describe(found):
    """`stage, detail (hint): last stderr line`, for the log line."""
    text = '%s, %s' % (found['stage'], found['detail'])
    if found.get('hint'):
        text += ' (%s)' % found['hint']
    if found.get('stderr'):
        text += ': ' + found['stderr']
    return text


def last_line(text):
    if isinstance(text, bytes):
        text = text.decode('utf-8', 'replace')
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    return lines[-1][:200] if lines else None


def ssh_hint(stderr):
    lowered = (stderr or '').lower()
    for needles, hint in SSH_HINTS:
        if any(needle in lowered for needle in needles):
            return hint
    return None


def mux_state(worker, *, run=subprocess.run, timeout=MUX_CHECK_SECONDS):
    """Does the ssh control master answer? `-O check` asks only the local socket.

    A check that itself times out is a wedged master; one that finds no master
    means the probe had to open a fresh connection. None when there is no link.
    """
    link = getattr(worker, 'link', None)
    options = getattr(link, 'options', None)
    if not options:
        return None
    try:
        proc = run(['ssh', *options, '-O', 'check', link.host],
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 'no answer from the control master within %g s (wedged)' % timeout
    except OSError as error:
        return 'unchecked (%s)' % error
    if proc.returncode == 0:
        return 'master running'
    return 'no master' + (': ' + last_line(proc.stderr) if last_line(proc.stderr) else '')


def host_pressure(gate=None, *, reader=None, wait=PRESSURE_WAIT_SECONDS):
    """This host's memory and load right now, read on a side thread.

    The reading is the pressure gate's own (`pressure.read_host`, or the gate's
    reader), taken fresh without touching the gate's state. Swap growth is
    measured against the gate's last sample. A reading that does not arrive
    within `wait` is itself the finding: this host is slow to run a subprocess.
    """
    read = reader or getattr(gate, 'reader', None) or pressure.read_host
    box = {}

    def sample():
        try:
            box['reading'] = read() or {}
        except Exception as error:            # noqa: BLE001 - a diagnostic, never a crash
            box['error'] = error
    thread = threading.Thread(target=sample, daemon=True, name='pandora-probe-pressure')
    thread.start()
    thread.join(wait)
    if 'error' in box:
        return 'unread (%s)' % type(box['error']).__name__
    if 'reading' not in box:
        return 'unread after %g s (slow to run a subprocess)' % wait
    reading = box['reading']
    parts = []
    swap = reading.get('swap_used_mib')
    if swap is not None:
        text = 'swap %.0f MiB' % swap
        previous = getattr(gate, 'previous', None)
        if previous and previous[1] is not None:
            span = gate.clock() - previous[0]
            if span >= 1.0:
                text += ' (%+.0f MiB/min over %.0f s)' % ((swap - previous[1]) * 60.0 / span,
                                                          span)
        parts.append(text)
    if reading.get('free_percent') is not None:
        parts.append('%.1f%% free' % reading['free_percent'])
    if reading.get('psi_full_avg10') is not None:
        parts.append('PSI full %.1f' % reading['psi_full_avg10'])
    if reading.get('load_per_cpu') is not None:
        parts.append('load %.2f/core' % reading['load_per_cpu'])
    evidence = getattr(gate, 'evidence', None)
    if evidence:
        parts.append('local gate closed: %s' % evidence)
    return ', '.join(parts) or 'no reading'


def transitions(before, after):
    """[(title, message)] for what changed between two readings.

    Edges only: the worker coming back, the worker going away, the worker going
    degraded for a reason the specific edges do not name, a canary that started
    failing, a pool nearing or crossing its floor, a kernel that changed. A
    state that has not changed is not news, and a monitor that re-announces
    every minute is a monitor nobody reads.
    """
    out = []
    was, now = before.get('worker', 'unknown'), after.get('worker', 'unknown')
    if now == 'down' and was in ('reachable', 'degraded'):
        out.append(('pandora: worker down', after.get('reason') or 'the engine did not answer'))
    elif now in ('reachable', 'degraded') and was == 'down':
        out.append(('pandora: worker back', 'the engine answered; state %s' % now))
    named = False
    if failed(after.get('canary')) and not failed(before.get('canary')):
        out.append(('pandora: worker canary failed',
                    'the worker last proved itself with %s failure(s)'
                    % (after['canary'] or {}).get('failures')))
        named = True
    if after.get('disk') == 'below floor' and before.get('disk') != 'below floor':
        out.append(('pandora: worker disk floor',
                    'the pool is below its floor; new runs are being refused'))
        named = True
    if after.get('kernel_drift') and not before.get('kernel_drift'):
        out.append(('pandora: worker kernel drift',
                    'the kernel changed since the canary passed; re-run '
                    '`pandora worker canary --mark`'))
        named = True
    if now == 'degraded' and was not in ('degraded', 'down') and not named:
        # The specific edges already said their piece; this is for the
        # degradations none of them cover, like a state.json that is not ready.
        reason = after.get('reason')
        if not reason and after.get('ready') not in (None, 'ready'):
            reason = 'worker state is %s' % after['ready']
        out.append(('pandora: worker degraded',
                    reason or 'the engine answered but is not fit'))
    if near_floor(after) and not near_floor(before):
        out.append(('pandora: worker disk low',
                    '%.1f GiB free of a %.1f GiB floor; the floor is close'
                    % (after['free_gib'], after['floor_gib'])))
    return out


def failed(canary):
    return bool(canary) and canary.get('ok') is False


def near_floor(reading):
    """Above the floor but inside the warn margin. Below floor is its own edge."""
    free, floor = reading.get('free_gib'), reading.get('floor_gib')
    return (isinstance(free, (int, float)) and isinstance(floor, (int, float))
            and 0 < floor <= free < floor * DISK_WARN_FACTOR)
