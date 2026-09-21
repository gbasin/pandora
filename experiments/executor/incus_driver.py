"""An Executor over the `incus` CLI, running on the worker itself.

Why on the worker and not over SSH: the memory watchdog samples the instance
cgroup several times a second and `execute` polls a log file at the same rate.
An SSH round trip on this host is ~90 ms, so a remote driver would spend more
time in transport than in work and would make a 0.08 s clone unmeasurable.
The control plane instead ships this directory as a content-addressed bundle
over SSH stdin (the v0.1.1 `worker_bundle` idea, see `bundle.py`) and makes one
SSH call per operation, each of which runs the whole operation locally.

Everything a run needs lives under /pandora inside the instance and under
$ROOT/runs/<run_id> on the worker.
"""
import glob
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

from interface import (Executor, Golden, Instance, Limits, Receipt, Result, Usage,
                       BackendUnavailable, CloneFailed, DestroyIncomplete,
                       ExecutionFailed, InstanceLost, MemoryExceeded, PrepareFailed)

NAME = re.compile('[a-z0-9][a-z0-9-]{0,50}[a-z0-9]')
GUEST = '/pandora'


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
                 root=None, sudo=True, sample_interval=0.25,
                 pressure_seconds=20.0, pressure_full=80.0):
        self.project, self.pool, self.profile = project, pool, profile
        self.root = Path(root or (Path.home() / 'incus-exec'))
        self.base = (['sudo'] if sudo else []) + ['incus', '--project', project]
        self.sample_interval = sample_interval
        # A thrash episode is "the cgroup spent most of the last window stalled
        # on memory and made no forward progress". Both halves are required.
        self.pressure_seconds = pressure_seconds
        self.pressure_full = pressure_full

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
        self.incus('launch', toolchain.base_image, name, '-p', self.profile, timeout=900)
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
            self.sh(name, 'docker pull -q %s' % shlex.quote(image), timeout=1800)
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
            self.incus('config', 'device', 'add', name, 'srcro', 'disk',
                       'source=' + str(source), 'path=/srcro', 'readonly=true', timeout=300)
            self.sh(name, 'mkdir -p %s && rsync -a --delete --exclude node_modules '
                          '--exclude .git /srcro/ %s/' % (shlex.quote(dest), shlex.quote(dest)),
                    timeout=1800)
            self.incus('config', 'device', 'remove', name, 'srcro', check=False, timeout=300)
        else:
            raise ValueError('unknown injection method ' + method)
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
        if limits:
            self.apply(name, limits)
        mark = time.monotonic()
        self.incus('start', name, timeout=300)
        self.wait_ready(name)
        return Instance(name=name, run_id=run_id, golden=golden.name,
                        clone_seconds=copied, start_seconds=time.monotonic() - mark)

    def apply(self, name, limits):
        """Admit on memory, CPU soft.

        `limits.memory.enforce=hard` writes memory.max; CPU is a weight and
        never a quota, so a run alone on the box gets the whole box.
        """
        self.incus('config', 'set', name,
                   'limits.memory=%dMiB' % limits.ceiling_mib,
                   'limits.memory.enforce=hard',
                   'limits.memory.swap=false',
                   'limits.cpu.priority=%d' % max(0, min(10, limits.cpu_weight // 10)))

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
                reattach=False):
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
            script = '\n'.join(
                ['#!/bin/bash', 'cd %s' % shlex.quote(cwd)] +
                ['export %s=%s' % (k, shlex.quote(str(v))) for k, v in sorted(env.items())] +
                ['systemctl start docker >/dev/null 2>&1 || true',
                 'for i in $(seq 100); do docker info >/dev/null 2>&1 && break; sleep 0.2; done',
                 'exec ' + ' '.join(shlex.quote(a) for a in argv)])
            self.incus('exec', name, '--', 'bash', '-c',
                       'mkdir -p %s && rm -f %s/rc %s/log && cat > %s/cmd.sh' % (GUEST, GUEST, GUEST, GUEST),
                       stdin=script.encode(), timeout=120)
            # setsid + its own pgid: the watchdog kills the group, not one pid.
            self.incus('exec', name, '--', 'bash', '-c',
                       'setsid bash -c \'bash %s/cmd.sh > %s/log 2>&1; echo $? > %s/rc\' '
                       '< /dev/null > /dev/null 2>&1 & echo $! > %s/pgid' % (GUEST, GUEST, GUEST, GUEST),
                       timeout=120)
        return self.supervise(instance, limits, on_log)

    def supervise(self, instance, limits, on_log=None):
        name, t0, offset = instance.name, time.monotonic(), 0
        samples, peak, evidence = [], 0, {}
        stall_since, outcome, code = None, None, None
        deadline = t0 + limits.wall_seconds
        while True:
            rc, out, _ = self.incus('exec', name, '--', 'bash', '-c',
                                    'tail -c +%d %s/log 2>/dev/null; echo "--RC--"; '
                                    'cat %s/rc 2>/dev/null' % (offset + 1, GUEST, GUEST),
                                    check=False, timeout=120)
            if rc != 0:
                if not self.exists(name):
                    raise InstanceLost('instance %s vanished mid-run' % name)
            else:
                chunk, _, tail = out.rpartition('--RC--')
                if chunk:
                    offset += len(chunk.encode())
                    if on_log:
                        on_log(chunk)
                if tail.strip().isdigit():
                    code = int(tail.strip())

            use = self.usage(instance)
            peak = max(peak, use.memory_peak or use.memory_current)
            samples.append({'t': round(time.monotonic() - t0, 2),
                            'mem': use.memory_current, 'peak': use.memory_peak,
                            'max_events': use.events.get('max', 0),
                            'oom': use.events.get('oom', 0),
                            'oom_kill': use.events.get('oom_kill', 0),
                            'psi_full10': use.pressure.get('memory_full_avg10', 0.0),
                            'cpu_usec': use.cpu_usec})
            if code is not None:
                outcome = 'ok' if code == 0 else 'failed'
                break

            # Kernel did the killing: believe it.
            if use.events.get('oom_kill', 0) > 0:
                outcome, evidence = 'oom', {'reason': 'oom_kill', 'events': use.events}
                break
            # Kernel did not, and the cgroup is wedged in reclaim. A hard cap
            # is not self-terminating; this is the watchdog the spike asked for.
            full = use.pressure.get('memory_full_avg10', 0.0)
            if full >= self.pressure_full and use.memory_current >= 0.95 * (use.memory_max or 1 << 62):
                stall_since = stall_since or time.monotonic()
                if time.monotonic() - stall_since >= self.pressure_seconds:
                    outcome = 'oom'
                    evidence = {'reason': 'memory-stall',
                                'psi_full_avg10': full,
                                'stalled_seconds': round(time.monotonic() - stall_since, 1),
                                'memory_current': use.memory_current,
                                'memory_max': use.memory_max,
                                'events': use.events}
                    break
            else:
                stall_since = None
            if time.monotonic() > deadline:
                outcome, evidence = 'timeout', {'reason': 'wall', 'seconds': limits.wall_seconds}
                break
            time.sleep(self.sample_interval)

        if outcome in ('oom', 'timeout'):
            self.kill(instance)
            code = -9
        seconds = time.monotonic() - t0
        final = self.usage(instance)
        evidence['samples'] = samples[-40:]
        evidence['sample_count'] = len(samples)
        return Result(exit_code=code if code is not None else -1, outcome=outcome,
                      seconds=seconds, usage=final, log_bytes=offset, evidence=evidence)

    def kill(self, instance):
        """Kill the run's process group, leaving the instance inspectable."""
        self.incus('exec', instance.name, '--', 'bash', '-c',
                   'p=$(cat %s/pgid 2>/dev/null); [ -n "$p" ] && kill -9 -"$p" 2>/dev/null; '
                   'pkill -9 -f journey-runner; true' % GUEST, check=False, timeout=60)

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
