"""The daemon's second lane: jobs that are heavy but stay on this Mac.

The remote lane exists because some work does not belong on a laptop. The local
lane exists because the rest of the work still has to be *scheduled* -- twelve
agents in twelve worktrees typing `pnpm check` at once is how the Mac died
before any of this, and moving journeys to a worker does not fix it. So a job
may declare `where = "local"` and get the same treatment as a routed one: one
queue, one ledger, one receipt, one `pandora ps`, one exit-code contract.

What this module is, in one line each:

* **Admission, not slots.** Pueue counted jobs per named group. This holds
  memory against a host budget and learns each (repo, job)'s peak from what it
  actually used, reusing `engine.admission` unchanged -- the same policy that
  admits on the worker, pointed at this machine's RAM minus a reserve. CPU is
  soft: a hint in `PANDORA_CPUS`, never a cgroup.
* **A process group, supervised.** The command runs with `start_new_session`,
  so a cancel reaches the whole tree with `killpg` and a stack that spawns
  Docker cannot survive its supervisor by being a grandchild.
* **Two exclusivity rules, both optional.** One active local run per worktree
  (Pueue's symlink reservation, without the symlink), and `singleton` jobs that
  own the machine -- which is what `dev:stack` is.
* **Drift, before and after.** The worktree is frozen into a manifest at the
  start and again at the end. An edit during the run means the verdict is about
  a tree that no longer exists, and the configuration decides whether that is a
  note or a refusal.

The one invariant the rest of the design leans on holds here too: `accepted` is
sent only after admission. Queueing, refusal and the repository's own validator
all happen before it, so all of them are provably non-executing.
"""
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from ..engine.admission import Admission, Store
from ..errors import PandoraError, SnapshotError
from ..exits import CANCELLED, INFRA, STALE
from ..snapshot.freeze import freeze

RESULT_VERSION = 1
SAMPLE_SECONDS = 1.0
KILL_GRACE_SECONDS = 15.0
# Handed to the child as a hint, never enforced. Mirrors the worker's rule:
# cores divided by admitted runs, floor one.
CORES = os.cpu_count() or 4


class Busy(PandoraError):
    """An exclusivity rule refused this run. Nothing started; retrying is fine.

    Deliberately not a pre-accept fallback code: running it locally anyway is
    exactly what the rule exists to prevent, so the caller gets exit 75 and the
    reason rather than a second copy of the job.
    """


def total_memory_mib():
    try:
        return (os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')) // 1048576
    except (ValueError, OSError, AttributeError):      # pragma: no cover - exotic platform
        return 8192


def budget_from(config):
    """The local budget in MiB: what is configured, or RAM minus the reserve."""
    stated = int(config.get('budget_mib') or 0)
    if stated > 0:
        return stated
    return max(1024, total_memory_mib() - int(config.get('reserve_mib') or 4096))


def group_rss_mib(pgid, run=subprocess.run):
    """Resident memory of a whole process group, in MiB.

    `ps` rather than anything cleverer because the thing being measured is a
    tree of node, pnpm, vitest workers and possibly Docker clients, and the only
    portable question with an answer is "what does the OS say this pgid holds".
    """
    try:
        proc = run(['ps', '-Ao', 'pgid=,rss='], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return 0
    total = 0
    want = str(pgid)
    for line in (proc.stdout or '').splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == want:
            try:
                total += int(parts[1])
            except ValueError:
                continue
    return total // 1024                                # ps reports KiB


class Budget:
    """One queue for this machine: memory admission plus the exclusivity rules.

    Everything is guarded by one condition variable, because a release has to
    wake whoever is waiting for the memory it just gave back. The daemon is the
    only process that admits, so in-process state is the whole truth -- unlike
    the fallback budget, which is file locks precisely because its commonest
    caller is a shim running when the daemon is gone.
    """

    def __init__(self, config, *, store_path=None, store=None):
        self.config = dict(config)
        self.admission = Admission(
            budget_mib=budget_from(config),
            store=store if store is not None else Store(str(store_path or ':memory:')),
            max_running=int(config.get('max_running') or 4))
        self.lock = threading.Lock()
        self.wake = threading.Condition(self.lock)
        self.worktrees = {}                 # resolved worktree -> run id
        self.singletons = {}                # job id -> run id
        self.held = {}                      # run id -> {'worktree','job','admitted'}

    # -- the rules that refuse rather than queue ---------------------------

    def reserve(self, run_id, *, repo, job, worktree, singleton):
        """Take the exclusive holds, or raise `Busy`. Never blocks.

        These are refusals, not queue positions, and on purpose: an agent whose
        second command waits silently behind its first one learns nothing, while
        exit 75 and a sentence tells it to look at what it already started.
        """
        key = str(Path(worktree).resolve())
        with self.lock:
            if singleton and job in self.singletons:
                raise Busy('%s already runs on this machine as %s; stop it with '
                           '`pandora cancel %s`.' % (job, self.singletons[job],
                                                     self.singletons[job]))
            if self.config.get('one_active_per_worktree', True) and key in self.worktrees:
                raise Busy('this worktree already has an active local job (%s). Inspect it '
                           'with `pandora ps`, or cancel it, before starting another.'
                           % self.worktrees[key])
            self.held[run_id] = {'worktree': key, 'job': job, 'repo': repo,
                                 'singleton': singleton, 'admitted': False}
            if singleton:
                self.singletons[job] = run_id
            if self.config.get('one_active_per_worktree', True):
                self.worktrees[key] = run_id

    # -- the rule that queues ----------------------------------------------

    def admit(self, run_id, *, repo, job, cancelled=None, timeout=0.0, poll=0.25):
        """Block until the memory fits, then return the admission record.

        Returns None if the caller cancelled or the deadline passed, which are
        both still pre-accept: nothing has run.
        """
        deadline = (time.monotonic() + timeout) if timeout else None
        with self.lock:
            reservation = self.admission.reservation(repo, job)[0]
            if reservation > self.admission.budget_mib:
                # Waiting for room that can never exist is worse than saying so:
                # the queue would hold this job until the daemon restarts.
                raise Busy('%s reserves %d MiB and the local budget is %d MiB. Raise '
                           '[local] budget_mib, lower reserve_mib, or give the job a '
                           'smaller size class.'
                           % (job, reservation, self.admission.budget_mib))
            while True:
                if cancelled is not None and cancelled():
                    return None
                verdict = self.admission.admit(run_id, repo, job)
                if verdict['admitted']:
                    self.held[run_id]['admitted'] = True
                    verdict['cpus_hint'] = max(1, CORES // max(1, len(self.admission.running)))
                    return verdict
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                self.wake.wait(poll)

    def finish(self, run_id, peak_mib, outcome):
        """Release everything this run held and teach the next one."""
        learned = None
        with self.lock:
            record = self.held.pop(run_id, None)
            if record is None:
                return None
            if record['admitted']:
                learned = self.admission.finish(run_id, max(0, int(peak_mib)), outcome)
            if record['singleton'] and self.singletons.get(record['job']) == run_id:
                del self.singletons[record['job']]
            if self.worktrees.get(record['worktree']) == run_id:
                del self.worktrees[record['worktree']]
            self.wake.notify_all()
        return learned

    def snapshot(self):
        with self.lock:
            return {'budget_mib': self.admission.budget_mib,
                    'held_mib': self.admission.held(),
                    'running': sorted(self.held),
                    'worktrees': dict(self.worktrees),
                    'singletons': dict(self.singletons)}


def child_environment(plan, request_env, *, cpus_hint, run_id, directory):
    """What the command sees. A denylist of this Mac, plus what the job declares.

    `PANDORA_ROUTE_DEPTH` is the one line that matters: without it a job whose
    command is itself `pnpm something` re-enters the shim and is routed a second
    time as if a person had typed it.
    """
    env = {}
    for name in ('PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'LC_ALL',
                 'TMPDIR', 'TERM', 'JAVA_HOME', 'DEVELOPER_DIR', 'DOCKER_HOST',
                 'DOCKER_CONTEXT'):
        if os.environ.get(name) is not None:
            env[name] = os.environ[name]
    for name in plan.get('env_passthrough', []):
        if name in (request_env or {}):
            env[name] = request_env[name]
    env.update(plan.get('env') or {})
    for name in plan.get('env_unset') or []:
        env.pop(name, None)
    env.update({'PANDORA_ROUTE_DEPTH': '1', 'PANDORA_LOCAL': '1',
                'PANDORA_RUN': run_id, 'PANDORA_RUN_DIR': str(directory),
                'PANDORA_CPUS': str(cpus_hint)})
    return env


class Supervisor:
    """One local command, its process group, its peak and its verdict."""

    def __init__(self, argv, *, cwd, env, timeout_seconds, on_log, on_tick=None):
        self.argv = list(argv)
        self.cwd = str(cwd)
        self.env = dict(env)
        self.timeout_seconds = timeout_seconds
        self.on_log = on_log
        self.on_tick = on_tick
        self.peak_mib = 0
        self.proc = None
        self.killed_at = None

    def signal_group(self, number):
        if self.proc is None or self.proc.pid is None:
            return
        try:
            os.killpg(self.proc.pid, number)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _pump(self, stream, which):
        try:
            while True:
                chunk = stream.read1(65536) if hasattr(stream, 'read1') else stream.read(65536)
                if not chunk:
                    return
                self.on_log(which, chunk)
        except (OSError, ValueError):
            return
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _sample(self, stop):
        while not stop.wait(SAMPLE_SECONDS):
            if self.proc is None:
                continue
            self.peak_mib = max(self.peak_mib, group_rss_mib(self.proc.pid))

    def run(self, cancelled):
        """Returns (outcome, exit_code). `outcome` is the engine's vocabulary."""
        try:
            self.proc = subprocess.Popen(
                self.argv, cwd=self.cwd, env=self.env, start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as error:
            self.on_log('err', ('pandora: cannot start %r: %s\n'
                                % (self.argv[0], error)).encode())
            return 'infra_failed', None
        stop = threading.Event()
        threads = [threading.Thread(target=self._pump, args=(self.proc.stdout, 'out'), daemon=True),
                   threading.Thread(target=self._pump, args=(self.proc.stderr, 'err'), daemon=True),
                   threading.Thread(target=self._sample, args=(stop,), daemon=True)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + self.timeout_seconds if self.timeout_seconds else None
        asked, timed_out = False, False
        try:
            while True:
                try:
                    self.proc.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if self.on_tick is not None:
                    self.on_tick(self)
                over = deadline is not None and time.monotonic() > deadline
                if (cancelled() or over) and not asked:
                    asked, timed_out = True, over
                    self.killed_at = time.monotonic()
                    self.signal_group(signal.SIGTERM)
                elif asked and time.monotonic() - self.killed_at > KILL_GRACE_SECONDS:
                    # The grace is over. SIGKILL the group, not the child: the
                    # child is usually a shell whose death orphans the tree.
                    self.signal_group(signal.SIGKILL)
                    self.killed_at = time.monotonic() + KILL_GRACE_SECONDS
        finally:
            stop.set()
            # Anything the tree left running keeps these pipes open; the readers
            # are daemon threads, so a stuck descendant cannot hold the daemon.
            for thread in threads:
                thread.join(timeout=2.0)
            self.signal_group(signal.SIGKILL)
        code = self.proc.returncode
        self.peak_mib = max(self.peak_mib, 0)
        if timed_out:
            return 'timed_out', code
        if asked:
            return 'cancelled', code
        return ('passed' if code == 0 else 'command_failed'), code


def cli_exit(outcome, exit_code):
    """The caller's exit. Never a passing code for a run without a verdict."""
    if outcome in ('passed', 'command_failed'):
        return exit_code if exit_code is not None else INFRA
    if outcome == 'cancelled':
        return CANCELLED
    if outcome == 'drifted':
        return STALE
    return INFRA


class LocalExecutor:
    """Admit, freeze, supervise, compare, publish. One method, in that order."""

    def __init__(self, budget, *, drift='warn', queue_timeout=0.0):
        self.budget = budget
        self.drift = drift
        self.queue_timeout = queue_timeout

    def fingerprint(self, worktree, plan):
        if self.drift == 'off':
            return None
        try:
            _manifest, _dropped, input_id = freeze(
                worktree, exclude_globs=plan.get('secrets_exclude_globs') or ())
            return input_id
        except (SnapshotError, OSError):
            return None

    def execute(self, run, plan, *, repo, job, worktree, request_env, admission,
                note=None, started=None):
        """Run one admitted local job and return its result dict.

        `admission` is the record the daemon already took, because admission has
        to happen before the client is told `accepted` and this method is called
        after it.
        """
        note = note or (lambda text: None)
        started = started if started is not None else time.time()
        before = self.fingerprint(worktree, plan)
        env = child_environment(plan, request_env, cpus_hint=admission.get('cpus_hint', 1),
                                run_id=run.id, directory=run.dir)
        supervisor = Supervisor(
            plan['argv'], cwd=Path(worktree) / (plan.get('cwd') or '.'), env=env,
            timeout_seconds=60 * int(plan.get('timeout_minutes') or 30),
            on_log=lambda which, chunk: run.stream_local(which, chunk))
        outcome, code = supervisor.run(run.cancelled.is_set)
        after = self.fingerprint(worktree, plan)
        drifted = before is not None and after is not None and before != after
        if drifted:
            if self.drift == 'fail':
                note('the worktree changed while this job ran, so its verdict describes a '
                     'tree that no longer exists. Re-run it.')
                if outcome == 'passed':
                    outcome = 'drifted'
            else:
                note('the worktree changed while this job ran; the verdict may not describe '
                     'the current tree.')
        learned = self.budget.finish(run.id, supervisor.peak_mib,
                                     {'passed': 'ok', 'command_failed': 'failed',
                                      'drifted': 'ok'}.get(outcome, 'lost'))
        finished = time.time()
        return {
            'version': RESULT_VERSION,
            'run_id': run.id,
            'lane': 'local',
            'repo': repo,
            'job': job,
            'argv': list(plan['argv']),
            'observed_exit': code,
            'cli_exit': cli_exit(outcome, code),
            'outcome': outcome,
            'layer': 'command' if outcome in ('passed', 'command_failed') else 'client',
            'peak_mib': supervisor.peak_mib,
            'reservation_mib': admission.get('reservation_mib'),
            'ceiling_mib': admission.get('ceiling_mib'),
            'size_class': admission.get('size_class'),
            'cpus_hint': admission.get('cpus_hint'),
            'source_before': before,
            'source_after': after,
            'drifted': drifted,
            'learned': learned,
            'created': started,
            'finished': finished,
            'wall_seconds': round(finished - started, 2),
        }
