"""The executor seam: what a run backend must implement, and nothing more.

A run is prepared once per (repo, install fingerprint), cloned per run,
given a repository if its job asked for one, executed, sampled, collected and
destroyed. Nothing here mentions containers, so a microVM driver can implement
the same operations.
"""
from dataclasses import dataclass, field, asdict
import hashlib
import json


class ExecutorError(RuntimeError):
    """Base class. Every failure a caller must distinguish is a subclass."""


class BackendUnavailable(ExecutorError):
    """The backend itself is not usable; no run may be admitted."""


class PrepareFailed(ExecutorError):
    """The golden instance could not be built for this fingerprint."""


class CloneFailed(ExecutorError):
    """A run instance could not be created from an existing golden."""


class InstanceLost(ExecutorError):
    """The instance named by the caller no longer exists on the backend."""


class ExecutionFailed(ExecutorError):
    """The backend could not start or supervise the command at all.

    A non-zero exit code from the command is not this; that is a Result.
    """


class DestroyIncomplete(ExecutorError):
    """Destroy ran but the receipt did not come back clean."""

    def __init__(self, message, receipt=None):
        self.receipt = receipt or {}
        super().__init__(message)


@dataclass(frozen=True)
class Toolchain:
    """Declarative description of what a golden instance contains.

    The fingerprint is the identity of the golden. Change any field and the
    next `prepare` builds a new golden rather than reusing the old one.
    """
    base_image: str = 'images:ubuntu/26.04'
    packages: tuple = ()
    node_version: str = ''
    pnpm_version: str = ''
    service_images: tuple = ()
    install_command: str = ''
    source_id: str = ''          # identity of the source tree baked in
    env: tuple = ()              # (key, value) pairs, sorted by the caller
    # The *resolved* inputs, as (key, value) pairs sorted by the caller:
    # `base_image` as an image fingerprint, `lockfile:<name>` as a content
    # digest, `service:<image>` as a registry manifest digest. The fields above
    # describe what to build; these say what the description resolved to on the
    # day it was resolved, which is the difference between two goldens with one
    # fingerprint and two goldens that are the same machine.
    pins: tuple = ()

    def fingerprint(self):
        body = asdict(self)
        # An *unpinned* toolchain hashes exactly as it did before pinning
        # existed, so adding the field does not orphan every golden on every
        # worker. A pinned one hashes differently on purpose.
        if not body['pins']:
            body.pop('pins')
        payload = json.dumps(body, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @property
    def pinned(self):
        return bool(self.pins)


@dataclass(frozen=True)
class Golden:
    """A warm, stopped instance plus the snapshot clones are taken from."""
    name: str
    fingerprint: str
    snapshot: str
    built_seconds: float = 0.0
    disk_bytes: int = 0
    reused: bool = False


@dataclass(frozen=True)
class Instance:
    """One run's private machine."""
    name: str
    run_id: str
    golden: str
    clone_seconds: float = 0.0
    start_seconds: float = 0.0


@dataclass(frozen=True)
class Limits:
    """Admit on memory, soft CPU.

    `memory_mib` is the learned reservation the scheduler admitted against;
    `ceiling_mib` is the hard cap for the size class. Exceeding the reservation
    is allowed and is fed back to admission; exceeding the ceiling is `oom`.
    `cpu_weight` is a share, never a quota. `cpus_hint` becomes PANDORA_CPUS.
    """
    memory_mib: int
    ceiling_mib: int
    cpu_weight: int = 100
    cpus_hint: int = 1
    wall_seconds: int = 1800
    # What a cancel sends before SIGKILL, and how long it waits. The instance is
    # destroyed either way; the grace is the run's chance to tear down a compose
    # stack or flush a trace first.
    cancel_signal: str = 'SIGKILL'
    cancel_grace_ms: int = 0
    # The run's own root-disk quota, enforced by the pool rather than watched.
    # 0 means unlimited, which is what every run had before this existed.
    disk_gib: int = 0


@dataclass(frozen=True)
class Usage:
    """Read from the instance cgroup, not from anything inside the run."""
    memory_current: int = 0
    memory_peak: int = 0
    memory_max: int = 0
    memory_high: int = 0
    swap_current: int = 0
    cpu_usec: int = 0
    events: dict = field(default_factory=dict)      # memory.events
    pressure: dict = field(default_factory=dict)    # {memory,cpu,io}.pressure
    processes: int = 0


@dataclass(frozen=True)
class Result:
    """What one `execute` produced.

    An over-ceiling run is `outcome='oom'` with `evidence`, never an exception:
    the POC declared a `MemoryExceeded` it never raised, and leaving both shapes
    in place was an ambiguity that would eventually get a caller wrong.
    """
    exit_code: int
    outcome: str          # ok | failed | oom | timeout | cancelled | lost
    seconds: float
    usage: Usage
    log_bytes: int = 0
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Receipt:
    """Machine-checkable proof that a run left nothing behind."""
    run_id: str
    instance: str
    seconds: float
    instance_gone: bool
    volume_gone: bool
    veth_gone: bool
    cgroup_gone: bool
    leftovers: tuple = ()

    @property
    def clean(self):
        return (self.instance_gone and self.volume_gone
                and self.veth_gone and self.cgroup_gone and not self.leftovers)


class Executor:
    """The operations. Implementations must raise the errors above."""

    def prepare(self, toolchain):
        """Return a Golden for this toolchain, building it if absent."""
        raise NotImplementedError

    def clone(self, golden, run_id):
        """Return a started Instance copy-on-write from `golden`."""
        raise NotImplementedError

    def synthetic_git(self, instance, dest, marks, message):
        """Make `dest` a one-commit repository whose index is the caller's
        tracked set: `marks` names the untracked and the tracked-but-ignored
        paths. Returns seconds; raises ExecutionFailed.
        """
        raise NotImplementedError

    def execute(self, instance, argv, env, cwd, limits, on_log=None):
        """Run argv, stream log lines to `on_log`, return a Result.

        Must survive the caller's transport dropping: the command keeps
        running and a later call with the same run can reattach to its log.
        """
        raise NotImplementedError

    def usage(self, instance):
        """Return a Usage sampled from the instance cgroup."""
        raise NotImplementedError

    def collect(self, instance, paths, into):
        """Copy `paths` out of the instance; return {path: local_path}."""
        raise NotImplementedError

    def destroy(self, instance):
        """Remove the instance and return a Receipt."""
        raise NotImplementedError
