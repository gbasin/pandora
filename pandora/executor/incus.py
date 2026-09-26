"""An Executor over the `incus` CLI, running on the worker itself.

Why on the worker and not over SSH: the memory watchdog samples the instance
cgroup several times a second and `execute` polls a log file at the same rate.
An SSH round trip on this host is ~90 ms, so a remote driver would spend more
time in transport than in work and would make a 0.08 s clone unmeasurable.
The control plane instead ships this directory to the worker — the v0.1.1
`worker_bundle.py` idea, a content-addressed payload over SSH stdin, verified
before use — and makes one SSH call per operation, each of which runs the
whole operation locally. This POC ships it with `rsync -az`; nothing measured
here depends on which of the two does the shipping.

Everything a run needs lives under /pandora inside the instance.
"""
import glob
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

from .interface import (Executor, Golden, Instance, Limits, Receipt, Result, Usage,
                        CloneFailed, DestroyIncomplete, ExecutionFailed,
                        ExecutorError, InstanceLost, PrepareFailed)

NAME = re.compile('[a-z0-9][a-z0-9-]{0,50}[a-z0-9]')
GUEST = '/pandora'


def untagged(image):
    """`postgres:16` -> `postgres`, leaving a registry's port alone."""
    host, _, last = image.rpartition('/')
    return '%s/%s' % (host, last.split(':')[0]) if host else last.split(':')[0]


def run(argv, *, timeout=600, check=True, stdin=None, capture=True):
    """One subprocess. Never a shell unless the caller wrote the shell line."""
    proc = subprocess.run(argv, input=stdin, timeout=timeout,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)
    out = (proc.stdout or b'').decode('utf-8', 'replace')
    err = (proc.stderr or b'').decode('utf-8', 'replace')
    if check and proc.returncode != 0:
        raise ExecutionFailed('%s failed (%d): %s' % (argv[:3], proc.returncode, err.strip()[:600]))
    return proc.returncode, out, err


class IncusDriver(Executor):
    def __init__(self, *, project='pandora', pool='pandorapool', profile='runner',
                 root=None, sudo=True, sample_interval=0.5,
                 thrash_seconds=15.0, thrash_rate=500.0, thrash_pinned=0.95,
                 thrash_psi=1.0, thrash_window=5.0):
        self.project, self.pool, self.profile = project, pool, profile
        self.root = Path(root or (Path.home() / 'incus-exec'))
        self.base = (['sudo'] if sudo else []) + ['incus', '--project', project]
        self.sample_interval = sample_interval
        # A thrash episode is three things at once, sustained: the cgroup is
        # pinned at its effective wall, charges are being refused hundreds of
        # times a second, and the cgroup is actually stalled. Measured: a hog
        # produces 700-4,000 refused charges per second; a passing journey
        # produces none. PSI full avg10 is storage-dependent: a hog on a
        # loop-file pool reads 5-8 %, but on local NVMe page-ins resolve fast
        # enough that it plateaus around 2 %, at the old 2.0 threshold every
        # dip restarted the sustain clock and the verdict took up to 80 s
        # (#150). The threshold sits at 1.0 and `stalled_seconds` counts
        # wedged time inside a trailing window, so a dip pauses the clock
        # instead of zeroing it. PSI alone is not enough (a single-threaded
        # thrasher on four CPUs only reaches 8 %) and the event rate alone is
        # not enough (memory.high throttling produces a high rate whenever a
        # run is merely close to its ceiling).
        self.thrash_seconds = thrash_seconds
        self.thrash_rate = thrash_rate
        self.thrash_pinned = thrash_pinned
        self.thrash_psi = thrash_psi
        self.thrash_window = thrash_window

    # --- plumbing ----------------------------------------------------------

    def incus(self, *args, **kw):
        return run(self.base + list(args), **kw)

    def sh(self, name, script, *, env=None, timeout=1800, check=True, cwd=None):
        """Run a shell line inside an instance and wait for it."""
        argv = list(self.base) + ['exec', name]
        for key, value in sorted((env or {}).items()):
            argv += ['--env', '%s=%s' % (key, value)]
        if cwd:
            argv += ['--cwd', cwd]
        return run(argv + ['--', 'bash', '-lc', script], timeout=timeout, check=check)

    def exists(self, name):
        rc, _, _ = self.incus('info', name, check=False)
        return rc == 0

    def wait_ready(self, name, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc, _, _ = self.incus('exec', name, '--', 'test', '-e', '/run/systemd/system', check=False)
            if rc == 0:
                return True
            time.sleep(0.05)
        raise CloneFailed('instance %s did not become ready in %ss' % (name, timeout))

    def cgroup(self, name):
        """The instance's payload cgroup, discovered rather than guessed.

        Incus 6.0.5 on this host puts it at /sys/fs/cgroup/lxc.payload.<project>_<name>
        (not the incus.slice/incus-<name>.scope the Firecracker spike saw on a
        default-project instance), so try both and then glob.
        """
        for path in ('/sys/fs/cgroup/lxc.payload.%s_%s' % (self.project, name),
                     '/sys/fs/cgroup/lxc.payload.%s' % name,
                     '/sys/fs/cgroup/incus.slice/incus-%s-%s.scope' % (self.project, name),
                     '/sys/fs/cgroup/incus.slice/incus-%s.scope' % name):
            if os.path.isdir(path):
                return path
        found = glob.glob('/sys/fs/cgroup/*%s*' % name) or \
            glob.glob('/sys/fs/cgroup/**/*%s*.scope' % name, recursive=True)
        found = [p for p in found if 'monitor' not in p and os.path.isdir(p)]
        if not found:
            raise InstanceLost('no cgroup for instance %s' % name)
        return found[0]

    def veth(self, name):
        rc, out, _ = self.incus('config', 'get', name, 'volatile.eth0.host_name', check=False)
        return out.strip() if rc == 0 else ''

    # --- prepare -----------------------------------------------------------

    def golden_name(self, toolchain):
        return 'golden-' + toolchain.fingerprint()

    def prepare(self, toolchain, source=None, log=print):
        """Build (or reuse) the golden instance for this toolchain.

        `source` is a host directory baked in at the fingerprint's source_id so
        that `pnpm install --frozen-lockfile` and the image pulls are warm. A
        run still injects its own source over the top (see `inject`).
        """
        name = self.golden_name(toolchain)
        if not NAME.fullmatch(name):
            raise PrepareFailed('golden name %r is not an instance name' % name)
        if self.exists(name):
            rc, out, _ = self.incus('snapshot', 'list', name, '--format', 'csv', check=False)
            if rc == 0 and any(line.split(',')[0] == 'warm' for line in out.splitlines()):
                return Golden(name=name, fingerprint=toolchain.fingerprint(),
                              snapshot='warm', reused=True, disk_bytes=self.volume_bytes(name))
            self.incus('delete', '-f', name, check=False)

        marks, t0 = {}, time.monotonic()
        # A pinned toolchain launches the image *fingerprint*, not the alias:
        # `images:ubuntu/26.04` is whatever the image server published today,
        # and two goldens built a week apart from one alias are not the same
        # machine even though the description that built them is identical.
        pins = dict(toolchain.pins)
        base = toolchain.base_image
        if pins.get('base_image'):
            remote = base.split(':', 1)[0] if ':' in base else 'images'
            base = '%s:%s' % (remote, pins['base_image'])
        self.incus('launch', base, name, '-p', self.profile, timeout=900)
        self.wait_ready(name)
        marks['launch'] = time.monotonic() - t0

        # The managed bridge is IPv4-only but its dnsmasq answers AAAA, so apt
        # and curl must be forced to v4 or every fetch waits out a timeout.
        mark = time.monotonic()
        packages = ' '.join(toolchain.packages)
        self.sh(name, 'set -e\n'
                'echo \'Acquire::ForceIPv4 "true";\' > /etc/apt/apt.conf.d/99force-ipv4\n'
                'export DEBIAN_FRONTEND=noninteractive\n'
                'apt-get update -qq\n'
                'apt-get install -y -qq --no-install-recommends ' + packages, timeout=1800)
        if toolchain.node_version:
            self.sh(name, 'set -e\ncurl -4 -fsSL https://nodejs.org/dist/v%s/node-v%s-linux-x64.tar.xz '
                          '| tar -xJ -C /usr/local --strip-components=1' %
                          (toolchain.node_version, toolchain.node_version), timeout=900)
        if toolchain.pnpm_version:
            self.sh(name, 'npm install -g pnpm@%s >/dev/null && pnpm -v' % toolchain.pnpm_version, timeout=900)
        marks['toolchain'] = time.monotonic() - mark

        mark = time.monotonic()
        if source:
            self.inject(name, source, '/work', method='tar')
        marks['source'] = time.monotonic() - mark

        mark = time.monotonic()
        self.sh(name, 'set -e\n'
                'mkdir -p %s\n'
                'systemctl start docker\n'
                'for i in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 0.3; done\n'
                'docker info | grep -E "Storage Driver|Cgroup Version"' % GUEST, timeout=600)
        if toolchain.install_command:
            self.sh(name, 'set -e\ncd /work\n' + toolchain.install_command, timeout=3600)
        for image in toolchain.service_images:
            # Same rule one layer down: a pinned service image is pulled by
            # manifest digest, so `postgres:16` cannot become a different
            # postgres between two runs that claim one fingerprint.
            digest = pins.get('service:' + image)
            ref = ('%s@%s' % (untagged(image), digest)) if digest else image
            self.sh(name, 'docker pull -q %s' % shlex.quote(ref), timeout=1800)
            if digest:
                self.sh(name, 'docker tag %s %s' % (shlex.quote(ref), shlex.quote(image)),
                        timeout=300, check=False)
        marks['deps'] = time.monotonic() - mark

        mark = time.monotonic()
        self.sh(name, 'systemctl stop docker docker.socket || true', check=False, timeout=300)
        self.incus('stop', name, timeout=600)
        self.incus('snapshot', 'create', name, 'warm', timeout=600)
        marks['snapshot'] = time.monotonic() - mark
        total = time.monotonic() - t0
        log('golden %s built in %.1fs %s' % (name, total, json.dumps({k: round(v, 2) for k, v in marks.items()})))
        return Golden(name=name, fingerprint=toolchain.fingerprint(), snapshot='warm',
                      built_seconds=total, disk_bytes=self.volume_bytes(name))

    def qgroup(self, name):
        """(referenced, exclusive) bytes of an instance's btrfs subvolume.

        Incus's own volume state reports `usage: null` on a btrfs pool, so read
        the qgroup directly. `exclusive` is the number that matters for a clone:
        it is what the clone costs over the golden it shares extents with.
        """
        mount = '/var/lib/incus/storage-pools/%s' % self.pool
        rc, out, _ = run(['sudo', 'btrfs', 'qgroup', 'show', '--raw', mount], check=False, timeout=120)
        want = 'containers/%s_%s' % (self.project, name)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[3] == want:
                return int(parts[1]), int(parts[2])
        return 0, 0

    def volume_bytes(self, name):
        return self.qgroup(name)[0]

    # --- inventory and headroom --------------------------------------------

    def pool_mount(self):
        return '/var/lib/incus/storage-pools/%s' % self.pool

    def qgroups(self):
        """{path: (referenced, exclusive)} for every subvolume in the pool."""
        rc, out, _ = run(['sudo', 'btrfs', 'qgroup', 'show', '--raw', self.pool_mount()],
                         check=False, timeout=120)
        found = {}
        if rc != 0:
            return found
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0][:1].isdigit() and parts[1].isdigit():
                found[parts[3]] = (int(parts[1]), int(parts[2]))
        return found

    def pool_usage(self):
        """Total, used and free bytes of the pool, read from btrfs itself.

        `df` on a btrfs filesystem reports allocation, not what is available to
        a new file, and Incus's own volume state reports `usage: null` on this
        driver. `filesystem usage --raw` is the only number that answers "may I
        start another run".
        """
        rc, out, _ = run(['sudo', 'btrfs', 'filesystem', 'usage', '--raw', self.pool_mount()],
                         check=False, timeout=120)
        total = used = free = 0
        for line in out.splitlines():
            text = line.strip()
            if text.startswith('Device size:'):
                total = int(text.split()[-1])
            elif text.startswith('Used:'):
                used = int(text.split()[-1])
            elif text.startswith('Free (estimated):'):
                # "Free (estimated): <bytes> (min: <bytes>)" -- the third word.
                free = int(text.split()[2])
        if rc != 0:
            return {'ok': False, 'pool': self.pool, 'error': 'btrfs usage unreadable'}
        return {'ok': True, 'pool': self.pool, 'mount': self.pool_mount(),
                'total_bytes': total, 'used_bytes': used, 'free_bytes': free,
                'free_gib': round(free / (1 << 30), 2),
                'used_fraction': round(used / total, 4) if total else 0.0}

    def capacity(self, floor_gib=0):
        """May the box take another run? The engine's admission hook.

        Memory admission has a ledger to reason with; disk has none, because a
        run's appetite for disk is not learned anywhere. So this is a floor and
        nothing cleverer: below it, new runs are refused with the arithmetic in
        the refusal, and the runs already going are left alone to finish.
        """
        usage = self.pool_usage()
        if not usage.get('ok'):
            # Unreadable headroom is not a refusal: a pool that cannot be
            # measured would otherwise stop every run on the worker, which is a
            # worse failure than admitting one run too many.
            # `usage` carries its own `ok`, so it is spread first and the
            # verdict written over it -- the other order answers the question
            # "could the pool be read" when it was asked "may a run start".
            return {**usage, 'ok': True, 'measured': False, 'floor_gib': floor_gib}
        ok = usage['free_gib'] >= floor_gib
        answer = {**usage, 'ok': ok, 'measured': True, 'floor_gib': floor_gib}
        if not ok:
            answer['reason'] = ('pool %s has %.2f GiB free, below the %d GiB floor'
                                % (self.pool, usage['free_gib'], floor_gib))
        return answer

    def instances(self, *, check=False):
        """[{name, state, created}] for every instance in the project.

        By default a failed listing reads as an empty project, which is the
        right answer for a status line and the wrong one for anything that
        deletes what the listing does not name. `check=True` raises instead, so
        gc can tell "no instances" from "could not look" (#88).
        """
        rc, out, err = self.incus('list', '--format', 'csv', '-c', 'nsD', check=False,
                                  timeout=180)
        rows = []
        if rc != 0:
            if check:
                raise ExecutorError('incus list exited %d: %s' % (rc, (err or '').strip()[:200]))
            return rows
        for line in out.splitlines():
            parts = line.split(',')
            if len(parts) >= 2 and parts[0]:
                rows.append({'name': parts[0], 'state': parts[1],
                             'created': ','.join(parts[2:]).strip('"')})
        return rows

    # --- source injection --------------------------------------------------

    def inject(self, name, source, dest, method='tar'):
        """Put a host source tree into an instance. Returns seconds."""
        t0 = time.monotonic()
        if method == 'tar':
            # One stream, one process each side; no per-file round trip.
            tar = subprocess.Popen(['tar', '-C', str(source), '-cf', '-', '.'], stdout=subprocess.PIPE)
            argv = self.base + ['exec', name, '--', 'bash', '-lc',
                                'mkdir -p %s && tar -C %s -xf -' % (shlex.quote(dest), shlex.quote(dest))]
            proc = subprocess.run(argv, stdin=tar.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            tar.stdout.close(); tar.wait()
            if proc.returncode != 0:
                raise ExecutionFailed('tar injection failed: ' + proc.stderr.decode()[:400])
        elif method == 'push':
            self.sh(name, 'mkdir -p ' + shlex.quote(dest))
            self.incus('file', 'push', '-r', '-p', str(source) + '/.', name + dest, timeout=3600)
        elif method == 'device':
            # No copy at all: the host tree appears in the instance. Read-only
            # because two runs share it and an idmapped write would surprise.
            self.incus('config', 'device', 'add', name, 'src', 'disk',
                       'source=' + str(source), 'path=' + dest, 'readonly=true', timeout=300)
        elif method == 'device-rsync':
            # The usable shape of the above: mount the host tree read-only once
            # and rsync it over the golden's baked-in copy, so a run gets a
            # writable tree and pays only for what changed since the golden.
            #
            # By content, and without copying times. A file whose bytes equal
            # the golden's keeps the golden's mtime; a changed one gets now.
            # With `-a`'s times, a fresh `git worktree add` -- same bytes, new
            # mtimes -- handed every file a date after the golden's install,
            # and pnpm's verify-deps-before-run, which judges patches by mtime
            # alone, refused every `pnpm <script>` with "Patches were modified".
            # Measured on acme: 0.53 s, against 0.12 s for a tree whose
            # mtimes already matched.
            self.incus('config', 'device', 'add', name, 'srcro', 'disk',
                       'source=' + str(source), 'path=/srcro', 'readonly=true', timeout=300)
            self.sh(name, 'mkdir -p %s && rsync -a --no-times --checksum --delete '
                          '--exclude node_modules --exclude .git /srcro/ %s/'
                    % (shlex.quote(dest), shlex.quote(dest)), timeout=1800)
            self.incus('config', 'device', 'remove', name, 'srcro', check=False, timeout=300)
        elif method == 'device-rsync-over':
            # The same mount, grafting rather than replacing: no --delete, so a
            # tree laid over an already-injected source adds to it instead of
            # becoming it. This is how a shard receives what its parent's plan
            # step built without paying for a second copy of the whole worktree.
            self.incus('config', 'device', 'add', name, 'graft', 'disk',
                       'source=' + str(source), 'path=/graft', 'readonly=true', timeout=300)
            self.sh(name, 'mkdir -p %s && rsync -a /graft/ %s/'
                    % (shlex.quote(dest), shlex.quote(dest)), timeout=1800)
            self.incus('config', 'device', 'remove', name, 'graft', check=False, timeout=300)
        else:
            raise ValueError('unknown injection method ' + method)
        return time.monotonic() - t0

    # --- a repository for suites that ask git ---------------------------------

    GIT_SCRIPT = r"""set -e
cd "$1"
git config --system --add safe.directory '*'
rm -rf .git
export GIT_AUTHOR_NAME=pandora GIT_AUTHOR_EMAIL=pandora@localhost
export GIT_COMMITTER_NAME=pandora GIT_COMMITTER_EMAIL=pandora@localhost
export GIT_AUTHOR_DATE=2000-01-01T00:00:00Z GIT_COMMITTER_DATE=2000-01-01T00:00:00Z
git init -q -b main
git config core.looseCompression 0
git config gc.auto 0
git add -A
if [ -s "$2/untracked" ]; then
  git --literal-pathspecs rm -q --cached --ignore-unmatch --pathspec-from-file="$2/untracked" --pathspec-file-nul
fi
if [ -s "$2/ignored" ]; then
  git --literal-pathspecs add -f --pathspec-from-file="$2/ignored" --pathspec-file-nul
fi
git commit -q --no-verify --allow-empty -m "$3"
rm -rf "$2"
"""

    def synthetic_git(self, name, dest, marks, message):
        """Make `dest` a one-commit repository whose index is the caller's tracked set.

        `git add -A` over the injected tree, then the two exception lists the
        client froze (`snapshot.git_status`): untracked files leave the index,
        tracked-but-ignored ones join it. The commit is deterministic -- fixed
        author, date and message -- so equal inputs have an equal HEAD.

        Measured on the worker over acme (4,961 files, 375 MiB): `git add -A`
        is 9.1 s with git's default loose-object compression and 2.9-3.0 s with
        it off; the commit is 0.1 s and acme's whole fingerprint afterward is
        25 ms. The objects are uncompressed on purpose: they live exactly as long
        as the instance. Returns seconds.
        """
        t0 = time.monotonic()
        lists = GUEST + '/git-marks'
        for flag in ('untracked', 'ignored'):
            data = b''.join(path.encode() + b'\0' for path in marks.get(flag) or ())
            run(self.base + ['exec', name, '--', 'sh', '-c',
                             'mkdir -p %s && cat > %s/%s' % (lists, lists, flag)],
                stdin=data, timeout=120)
        rc, _, err = run(self.base + ['exec', name, '--', 'sh', '-c', self.GIT_SCRIPT,
                                      'git', dest, lists, message],
                         check=False, timeout=900)
        if rc != 0:
            raise ExecutionFailed('synthetic git in %s failed: %s' % (name, err.strip()[:400]))
        return time.monotonic() - t0

    # --- clone -------------------------------------------------------------

    def clone(self, golden, run_id, limits=None):
        name = 'run-' + run_id
        if not NAME.fullmatch(name):
            raise CloneFailed('run id %r is not an instance name' % run_id)
        t0 = time.monotonic()
        rc, _, err = self.incus('copy', '%s/%s' % (golden.name, golden.snapshot), name,
                                check=False, timeout=900)
        if rc != 0:
            raise CloneFailed('copy %s -> %s: %s' % (golden.name, name, err.strip()[:400]))
        copied = time.monotonic() - t0
        try:
            if limits:
                self.apply(name, limits)
            mark = time.monotonic()
            self.incus('start', name, timeout=300)
            self.wait_ready(name)
        except Exception as error:
            # A copy that could not be configured or started is not an
            # instance a run may use, and leaving it leaks the instance and
            # its volume for gc to find (#88). The delete is best-effort: the
            # original failure is the one the run needs to hear. A timeout
            # becomes CloneFailed so the failure maps to retryable
            # 'clone-failed' instead of 'engine-error'.
            try:
                self.incus('delete', '-f', name, check=False, timeout=300)
            except Exception:                                        # noqa: BLE001
                pass
            if isinstance(error, subprocess.TimeoutExpired):
                raise CloneFailed('%s timed out being made from %s'
                                  % (name, golden.name)) from error
            raise
        return Instance(name=name, run_id=run_id, golden=golden.name,
                        clone_seconds=copied, start_seconds=time.monotonic() - mark)

    def apply(self, name, limits):
        """Admit on memory, CPU soft.

        `limits.memory.enforce=hard` writes memory.max. CPU uses the
        *percentage* form of `limits.cpu.allowance`, which Incus writes to
        `cpu.weight` and leaves `cpu.max` unlimited — a share, not a quota, so
        a run alone on the box gets the whole box. `limits.cpu.priority` is
        not used: it only spans cpu.weight 90-100, which is not a usable
        differential (measured in §6).
        """
        self.incus('config', 'set', name,
                   'limits.memory=%dMiB' % limits.ceiling_mib,
                   'limits.memory.enforce=hard',
                   'limits.memory.swap=false',
                   'limits.cpu.allowance=%d%%' % max(1, min(100, limits.cpu_weight)))
        gib = getattr(limits, 'disk_gib', 0) or self.default_disk_gib()
        if gib:
            self.quota(name, gib)

    def default_disk_gib(self):
        """The worker's own per-run quota, written beside the ledger.

        Read here rather than plumbed through the scheduler because a disk
        quota is a property of the *machine* -- an operator's number, like the
        size classes -- and nothing about a run predicts it.
        """
        try:
            value = (self.root / 'run_disk_gib').read_text().strip()
        except OSError:
            return 0
        return int(value) if value.isdigit() else 0

    def quota(self, name, gib):
        """Cap what one run may write, enforced by the pool, not by a watchdog.

        The root disk comes from the profile, so it has to be *overridden* onto
        the instance before a size can be set on it; `config device set` alone
        answers "The profile device doesn't exist".

        **The number is total `referenced` bytes, not the run's own writes.**
        Incus's btrfs driver writes `btrfs qgroup limit <size>`, which limits
        *referenced*, and a clone references every extent it shares with its
        golden from the moment it exists. A 4.3 GiB golden under a 6 GiB quota
        therefore gives the run about 1.7 GiB of its own, and a quota below the
        golden's own size gives it a machine that cannot start -- btrfs refuses
        every later qgroup operation on an over-quota group, so Incus fails
        mid-way and leaves the instance unconfigurable. That is worth a legible
        refusal here rather than an unexplained one three commands later.
        """
        try:
            self.settle_qgroups()
            referenced = self.qgroup(name)[0]
        except subprocess.TimeoutExpired as error:
            raise CloneFailed('qgroup accounting on %s timed out while setting '
                              'up %s: %s' % (self.pool, name, error)) from error
        if referenced and gib * (1 << 30) <= referenced:
            raise CloneFailed(
                'disk quota %d GiB on %s is at or below the %.2f GiB it already '
                'references from its golden; the quota limits referenced bytes, '
                'so it must leave room above that' % (gib, name, referenced / (1 << 30)))
        rc, _, err = self.incus('config', 'device', 'override', name, 'root',
                                'size=%dGiB' % gib, check=False, timeout=300)
        if rc != 0:
            rc, _, err = self.incus('config', 'device', 'set', name, 'root',
                                    'size=%dGiB' % gib, check=False, timeout=300)
        if rc != 0:
            raise CloneFailed('disk quota %dGiB on %s: %s' % (gib, name, err.strip()[:200]))
        return gib

    def settle_qgroups(self):
        """Rescan the pool's qgroups when the kernel has stopped counting them.

        Deleting a large subvolume (a golden) whose tree is at least
        `qgroups/drop_subtree_threshold` levels deep (3 by default) makes the
        kernel mark qgroups inconsistent and skip accounting until a rescan,
        rather than trace the whole tree. Until then a clone's `referenced`
        never grows, so its limit is set and never reached: the 2026-09-23
        canary wrote 2 GiB into a 5 GiB quota over a 3.97 GiB golden. A limit
        that silently does nothing is refused here instead of handed out.
        """
        def inconsistent():
            rc, out, err = run(['sudo', 'btrfs', 'qgroup', 'show', '--raw', self.pool_mount()],
                               check=False, timeout=120)
            return 'inconsistent' in (out + err).lower()
        try:
            if not inconsistent():
                return False
            rc, _, err = run(['sudo', 'btrfs', 'quota', 'rescan', '-w', self.pool_mount()],
                             check=False, timeout=900)
            if rc != 0:     # one already running: wait for that one instead
                run(['sudo', 'btrfs', 'quota', 'rescan', '-W', self.pool_mount()],
                    check=False, timeout=900)
            if inconsistent():
                raise CloneFailed('btrfs qgroups on %s are inconsistent and a rescan did not '
                                  'settle them, so a disk quota would not be enforced: %s'
                                  % (self.pool, err.strip()[:200]))
        except subprocess.TimeoutExpired as error:
            raise CloneFailed('btrfs qgroup rescan on %s timed out: %s'
                              % (self.pool, error)) from error
        return True

    def harden(self, instance, limits):
        """Write the cgroup arrangement the memory investigation settled on.

        Incus sets memory.max; these three are what stops an over-limit run
        from livelocking in reclaim instead of dying:
          memory.swap.max=0    nothing to page out to, fail fast
          memory.high          throttle before the wall, so PSI rises early
          memory.oom.group=1   when the kernel does OOM, take the whole run
        Applied after start because the cgroup does not exist before it.
        """
        path = self.cgroup(instance.name)
        high = int(limits.ceiling_mib * 0.9) * 1024 * 1024
        written = {}
        for leaf, value in (('memory.swap.max', '0'),
                            ('memory.high', str(high)),
                            ('memory.oom.group', '1')):
            rc, _, err = run(['sudo', 'tee', os.path.join(path, leaf)],
                             stdin=value.encode(), check=False)
            written[leaf] = value if rc == 0 else 'ERR:' + err.strip()[:80]
        return written

    # --- execute -----------------------------------------------------------

    def execute(self, instance, argv, env=None, cwd='/work', limits=None, on_log=None,
                reattach=False, on_tick=None):
        """Start argv detached inside the instance and supervise it.

        Detached on purpose: the command's parent is the instance's own init,
        not `incus exec`, so losing the control connection (or this process)
        does not kill the run. A later call with reattach=True picks the same
        log and exit-code files back up.
        """
        limits = limits or Limits(memory_mib=2048, ceiling_mib=4096)
        name = instance.name
        env = dict(env or {})
        env.setdefault('PANDORA_CPUS', str(limits.cpus_hint))
        env.setdefault('PANDORA_RUN_ID', instance.run_id)
        if not reattach:
            self.start(name, argv, env, cwd)
        return self.supervise(instance, limits, on_log, on_tick=on_tick)

    def start(self, name, argv, env=None, cwd='/work', docker=True):
        """Write the run script and launch it detached under its own pgid.

        The argv never goes through a shell command line: it is quoted into a
        file that is piped in over stdin, so a journey's quoting is not the
        driver's problem.
        """
        script = '\n'.join(
            ['#!/bin/bash', 'cd %s' % shlex.quote(cwd)] +
            ['export %s=%s' % (k, shlex.quote(str(v))) for k, v in sorted((env or {}).items())] +
            (['systemctl start docker >/dev/null 2>&1 || true',
              'for i in $(seq 100); do docker info >/dev/null 2>&1 && break; sleep 0.2; done']
             if docker else []) +
            ['exec ' + ' '.join(shlex.quote(a) for a in argv)])
        self.incus('exec', name, '--', 'bash', '-c',
                   'mkdir -p %s && rm -f %s/rc %s/log && cat > %s/cmd.sh' % (GUEST, GUEST, GUEST, GUEST),
                   stdin=script.encode(), timeout=120)
        # setsid + its own pgid: the watchdog kills the group, not one pid.
        self.incus('exec', name, '--', 'bash', '-c',
                   'setsid bash -c \'bash %s/cmd.sh > %s/log 2>&1; echo $? > %s/rc\' '
                   '< /dev/null > /dev/null 2>&1 & echo $! > %s/pgid' % (GUEST, GUEST, GUEST, GUEST),
                   timeout=120)

    def poll(self, instance, offset, timeout):
        """Read new log bytes and the exit code from inside the instance.

        This is the only part of supervision that enters the instance, so it
        is the only part a memory-capped run can slow down: `incus exec` forks
        a process inside the cgroup being watched, and under `memory.high`
        throttling that process is deliberately made to crawl. It is given a
        short timeout and its failure is never fatal — the watchdog runs off
        host-side cgroup reads, which nothing inside the run can affect.
        """
        rc, out, _ = self.incus('exec', instance.name, '--', 'bash', '-c',
                                'tail -c +%d %s/log 2>/dev/null; echo "--RC--"; '
                                'cat %s/rc 2>/dev/null' % (offset + 1, GUEST, GUEST),
                                check=False, timeout=timeout)
        if rc != 0:
            if not self.exists(instance.name):
                raise InstanceLost('instance %s vanished mid-run' % instance.name)
            return None, None
        chunk, _, tail = out.rpartition('--RC--')
        return chunk, int(tail.strip()) if tail.strip().isdigit() else None

    def supervise(self, instance, limits, on_log=None, poll_timeout=20, on_tick=None):
        """Watch one running command until it has a verdict.

        `on_tick` is called once per host-side sample and may return `'cancel'`,
        which is how an outside decision (the ledger's `cancel_requested`) reaches
        a loop that is otherwise deliberately independent of everything but the
        cgroup. It is checked on the same schedule as the watchdog and for the
        same reason: nothing inside the run can delay it.
        """
        t0, offset = time.monotonic(), 0
        samples, peak, evidence = [], 0, {}
        outcome, code = None, None
        deadline = t0 + limits.wall_seconds
        next_poll, slow_polls = 0.0, 0
        while True:
            # Host side first and unconditionally: the verdict must not depend
            # on a probe the run can starve.
            use = self.usage(instance)
            if time.monotonic() - t0 >= next_poll:
                mark = time.monotonic()
                try:
                    chunk, code = self.poll(instance, offset, poll_timeout)
                except subprocess.TimeoutExpired:
                    chunk, code, slow_polls = None, None, slow_polls + 1
                spent = time.monotonic() - mark
                if chunk:
                    offset += len(chunk.encode())
                    if on_log:
                        on_log(chunk)
                if spent > 1.0:
                    slow_polls += 1
                # Back off the guest-side probe when the guest is struggling,
                # so supervision keeps sampling at full rate regardless.
                next_poll = (time.monotonic() - t0) + min(30.0, max(0.0, spent * 4))

            peak = max(peak, use.memory_peak or use.memory_current)
            samples.append({'t': round(time.monotonic() - t0, 2),
                            'mem': use.memory_current, 'peak': use.memory_peak,
                            # memory.high suppresses `max` entirely, so the
                            # watchdog counts both kinds of refused charge.
                            'throttle_events': use.events.get('max', 0) + use.events.get('high', 0),
                            'max_events': use.events.get('max', 0),
                            'high_events': use.events.get('high', 0),
                            'oom': use.events.get('oom', 0),
                            'oom_kill': use.events.get('oom_kill', 0),
                            'psi_full10': use.pressure.get('memory_full_avg10', 0.0),
                            'cpu_usec': use.cpu_usec})
            if code is not None:
                outcome = 'ok' if code == 0 else 'failed'
                break

            if on_tick is not None and on_tick() == 'cancel':
                outcome, evidence = 'cancelled', {'reason': 'cancel requested'}
                break

            # Kernel did the killing: believe it.
            if use.events.get('oom_kill', 0) > 0:
                outcome, evidence = 'oom', {'reason': 'oom_kill', 'events': use.events}
                break
            # Kernel did not, and the cgroup is wedged in reclaim. A hard cap
            # is not self-terminating; this is the watchdog the spike asked for.
            # Smoothed over a trailing window, not sample to sample: the
            # instantaneous rate of a real thrash swings between 160/s and
            # 1,500/s, so a per-sample threshold resets its own timer.
            rate, window = 0.0, samples[-1]['t'] - self.thrash_window
            older = next((s for s in samples if s['t'] >= window), samples[0])
            span = samples[-1]['t'] - older['t']
            if span > 0:
                rate = (samples[-1]['throttle_events'] - older['throttle_events']) / span
            # The effective wall is memory.high when it is set, because the
            # cgroup is reclaimed down to it and never reaches memory.max.
            wall = min(x for x in (use.memory_high, use.memory_max) if x) or (1 << 62)
            pinned = use.memory_current >= self.thrash_pinned * wall
            psi = use.pressure.get('memory_full_avg10', 0.0)
            samples[-1]['throttle_rate'] = round(rate, 1)
            samples[-1]['stalled'] = (pinned and rate >= self.thrash_rate
                                      and psi >= self.thrash_psi)
            stalled = self.stalled_seconds(samples)
            if stalled >= self.thrash_seconds:
                outcome = 'oom'
                evidence = {'reason': 'memory-thrash',
                            'throttle_events_per_second': round(rate, 1),
                            'threshold_per_second': self.thrash_rate,
                            'thrashing_seconds': round(stalled, 1),
                            'memory_current': use.memory_current,
                            'memory_wall': wall,
                            'memory_max': use.memory_max,
                            'memory_high': use.memory_high,
                            'psi_memory_some_avg10': use.pressure.get('memory_some_avg10', 0.0),
                            'psi_memory_full_avg10': psi,
                            'events': use.events}
                break
            if time.monotonic() > deadline:
                outcome, evidence = 'timeout', {'reason': 'wall', 'seconds': limits.wall_seconds}
                break
            time.sleep(self.sample_interval)

        if outcome in ('oom', 'timeout', 'cancelled'):
            # Only a cancel gets a grace. An `oom` or a wall timeout is a machine
            # that is already not working, and waiting politely on it is how a
            # thrashing instance holds the box for another four minutes.
            if outcome == 'cancelled':
                self.kill(instance, signal=limits.cancel_signal,
                          grace_ms=limits.cancel_grace_ms)
            else:
                self.kill(instance)
            code = -9
        seconds = time.monotonic() - t0
        try:
            final = self.usage(instance)
        except (InstanceLost, subprocess.TimeoutExpired):
            final = samples and Usage(memory_current=samples[-1]['mem'],
                                      memory_peak=samples[-1]['peak'],
                                      cpu_usec=samples[-1]['cpu_usec']) or Usage()
        evidence['samples'] = samples[-40:]
        evidence['sample_count'] = len(samples)
        evidence['slow_guest_polls'] = slow_polls
        return Result(exit_code=code if code is not None else -1, outcome=outcome,
                      seconds=seconds, usage=final, log_bytes=offset, evidence=evidence)

    def stalled_seconds(self, samples):
        """Wedged time inside the trailing window, twice the sustain bar.

        Counted, not streaked: a sample under any of the three thresholds
        pauses the clock without zeroing it, which is what a signal that sits
        at its threshold -- PSI on fast storage -- needs (#150).
        """
        if not samples:
            return 0.0
        horizon = samples[-1]['t'] - 2 * self.thrash_seconds
        total, next_t = 0.0, samples[-1]['t']
        for s in reversed(samples):
            if s['t'] < horizon:
                break
            if s.get('stalled'):
                total += next_t - s['t']
            next_t = s['t']
        return total

    def kill(self, instance, *, signal='SIGKILL', grace_ms=0):
        """Kill the run's process group, leaving the instance inspectable.

        `incus exec` into a thrashing instance is itself charged to the capped
        cgroup, so it can be slow; if it does not land, fall back to the host's
        own `cgroup.kill`, which needs nothing from inside.

        With a grace, the first signal is the job's own and SIGKILL follows only
        if the group is still alive when it expires. The escalation is not
        optional: a grace is how long a run may take to clean up, never whether
        it may decline to stop.
        """
        grace = max(0, int(grace_ms) // 1000)
        script = ('p=$(cat %s/pgid 2>/dev/null); [ -n "$p" ] || exit 0; '
                  'kill -%s -"$p" 2>/dev/null; '
                  'end=$(( $(date +%%s) + %d )); '
                  'while kill -0 -"$p" 2>/dev/null && [ "$(date +%%s)" -lt "$end" ]; '
                  'do sleep 0.5; done; '
                  'kill -9 -"$p" 2>/dev/null; true') % (GUEST, signal, grace)
        try:
            rc, _, _ = self.incus('exec', instance.name, '--', 'bash', '-c', script,
                                  check=False, timeout=30 + grace)
        except subprocess.TimeoutExpired:
            rc = -1
        if rc != 0:
            try:
                run(['sudo', 'tee', os.path.join(self.cgroup(instance.name), 'cgroup.kill')],
                    stdin=b'1', check=False, timeout=30)
            except (InstanceLost, subprocess.TimeoutExpired):
                pass

    # --- usage -------------------------------------------------------------

    def usage(self, instance):
        path = self.cgroup(instance.name)
        rc, out, _ = run(['sudo', 'bash', '-c',
                          'cd %s && for f in memory.current memory.peak memory.max memory.high '
                          'memory.swap.current memory.events memory.pressure cpu.pressure '
                          'io.pressure cpu.stat pids.current; do echo "==$f"; cat $f 2>/dev/null; done'
                          % shlex.quote(path)], check=False, timeout=60)
        if rc != 0:
            raise InstanceLost('cgroup for %s unreadable' % instance.name)
        return parse_cgroup(out)

    # --- collect / destroy -------------------------------------------------

    def collect(self, instance, paths, into):
        into = Path(into)
        into.mkdir(parents=True, exist_ok=True)
        got = {}
        for path in paths:
            target = into / Path(path).name
            rc, _, err = self.incus('file', 'pull', '-r', '%s%s' % (instance.name, path),
                                    str(into), check=False, timeout=900)
            got[path] = str(target) if rc == 0 and target.exists() else None
        return got

    def destroy(self, instance):
        name, t0 = instance.name, time.monotonic()
        veth = self.veth(name)
        cgroup = None
        try:
            cgroup = self.cgroup(name)
        except InstanceLost:
            pass
        rc, _, err = self.incus('delete', '-f', name, check=False, timeout=600)
        seconds = time.monotonic() - t0
        leftovers = []
        if rc != 0 and self.exists(name):
            leftovers.append('delete failed: ' + err.strip()[:200])
        instance_gone = not self.exists(name)
        _, vols, _ = self.incus('storage', 'volume', 'list', self.pool, '--format', 'csv', check=False)
        volume_gone = not any(line.split(',')[1:2] == [name] for line in vols.splitlines())
        if not volume_gone:
            leftovers.append('storage volume container/' + name)
        _, links, _ = run(['ip', '-o', 'link'], check=False)
        veth_gone = not veth or veth not in links
        if not veth_gone:
            leftovers.append('veth ' + veth)
        cgroup_gone = not cgroup or not os.path.isdir(cgroup)
        if not cgroup_gone:
            leftovers.append('cgroup ' + cgroup)
        receipt = Receipt(run_id=instance.run_id, instance=name, seconds=seconds,
                          instance_gone=instance_gone, volume_gone=volume_gone,
                          veth_gone=veth_gone, cgroup_gone=cgroup_gone,
                          leftovers=tuple(leftovers))
        if not receipt.clean:
            raise DestroyIncomplete('destroy of %s left objects' % name, receipt.__dict__)
        return receipt


def parse_cgroup(text):
    """Turn the concatenated cgroup files of `usage` into a Usage."""
    blocks, key = {}, None
    for line in text.splitlines():
        if line.startswith('=='):
            key = line[2:]
            blocks[key] = []
        elif key:
            blocks[key].append(line)

    def number(name):
        value = (blocks.get(name) or [''])[0].strip()
        return int(value) if value.isdigit() else 0

    def pairs(name):
        out = {}
        for line in blocks.get(name, []):
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip('-').isdigit():
                out[parts[0]] = int(parts[1])
        return out

    pressure = {}
    for resource in ('memory', 'cpu', 'io'):
        for line in blocks.get(resource + '.pressure', []):
            parts = line.split()
            if not parts:
                continue
            for item in parts[1:]:
                field, _, value = item.partition('=')
                try:
                    pressure['%s_%s_%s' % (resource, parts[0], field)] = float(value)
                except ValueError:
                    pass
    cpu = pairs('cpu.stat')
    return Usage(memory_current=number('memory.current'),
                 memory_peak=number('memory.peak'),
                 memory_max=number('memory.max'),
                 memory_high=number('memory.high'),
                 swap_current=number('memory.swap.current'),
                 cpu_usec=cpu.get('usage_usec', 0),
                 events=pairs('memory.events'),
                 pressure=pressure,
                 processes=number('pids.current'))
