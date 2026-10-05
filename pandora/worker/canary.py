"""What has to be true before this worker may be called `ready`.

The POC's canary proved the *driver*. This one proves the *worker*: the same
memory checks, plus the nested docker and compose stack a repository's own
services need, plus a real journey and a real surface command in clones of the
golden each enrolled repository's `pandora.toml` names, plus the bridge port
isolation between two concurrent clones (#172), the disk quota, the receipts
and the headroom.

Which golden, and which journey, come from the client: `pandora worker canary`
reads the enrolled repositories' `[worker]` tables and ships one target per
distinct recipe (`targets` below). The worker then pins each recipe as a routed
run would (`pinned_target`): against the target's source when it is on the
worker, so the canary proves the golden the next routed run clones, and
otherwise the newest golden routed runs pinned from that recipe. Before that, it proved whatever two
toolchain files it was handed, and on 2026-09-23 those were not the toolchain
the enrolled configuration ran on. The files survive as an override for a
worker nobody has enrolled against yet.

Budget: under four minutes per target, and there is one target per distinct
enrolled golden, so two toolchains cost up to eight. That is why the surfaces
check runs `--list` rather than a browser by default and why the memory hog is
given a 512 MiB ceiling it reaches in about six seconds. A gate nobody can afford to run
is a gate nobody runs.

Every check prints one row into the verdict: name, ok, detail, seconds. The
verdict's `ok` is false if any of them is false, and `pandora worker provision`
refuses to write `ready` when it is.
"""
import json
import shlex
import socket
import time
from pathlib import Path

from ..engine import pinning, turbocache
from ..engine.runner import Paths, toolchain_of
from . import goldens as golden_index
from ..executor.incus import IncusDriver
from ..executor.interface import DestroyIncomplete, Limits
from ..executor.memtest import hog

BUDGET_SECONDS = 240


class Checks:
    def __init__(self):
        self.started = time.monotonic()
        self.rows = []

    def add(self, name, ok, detail=''):
        self.rows.append({'check': name, 'ok': bool(ok), 'detail': str(detail)[:400],
                          'at': round(time.monotonic() - self.started, 1)})
        return bool(ok)

    @property
    def failures(self):
        return [row for row in self.rows if not row['ok']]

    @property
    def seconds(self):
        return round(time.monotonic() - self.started, 1)


def load_toolchain(path):
    return toolchain_of(json.loads(Path(path).read_text()))


# What the explicit toolchain files have always been asked to prove. Kept for
# `--journey` / `--surfaces`, the override for a worker no repository has been
# enrolled against yet; a derived target carries its own argv from the
# repository's `pandora.toml` instead.
LEGACY_JOURNEY = {'id': 'S0-01',
                  'argv': ['node', 'tools/validation/journey-runner.mjs', 'run', 'S0-01'],
                  'env': {'JOURNEY_REPLAY': 'cover'}, 'cwd': '/work',
                  'compose': 'tools/stack/compose.yml'}
# A listing, not a browser: `plan` reports the test IDs each shard would
# select, which exercises node, the workspace and the surface runner's own
# selection without paying for chromium. The four-minute budget does not
# survive a real surface run.
LEGACY_SURFACE = {'id': 'desk', 'step': 'plan',
                  'argv': ['node', 'tools/validation/surface-runner.mjs',
                           'plan', 'desk', '--shards', '1'],
                  'env': {}, 'cwd': '/work'}


def legacy_targets(journey=None, surfaces=None, source=None, journey_argv=None,
                   surfaces_argv=None):
    """The two explicit toolchain files as targets, one check each."""
    targets = []
    if journey:
        check = dict(LEGACY_JOURNEY)
        if journey_argv:
            check['argv'] = list(journey_argv)
        targets.append({'label': 'journeys', 'toolchain': json.loads(Path(journey).read_text()),
                        'source': source, 'journey': check, 'surface': None})
    if surfaces:
        check = dict(LEGACY_SURFACE)
        if surfaces_argv:
            check['argv'] = list(surfaces_argv)
        targets.append({'label': 'surfaces', 'toolchain': json.loads(Path(surfaces).read_text()),
                        'source': source, 'journey': None, 'surface': check})
    return targets


def pinned_target(paths, driver, recipe, source):
    """(spec, source-or-None) naming the golden a routed run would use now.

    With the source on the worker, the canary pins exactly as a supervisor
    does (`pinning.settle`), so it proves the golden the next routed run
    clones. Without it, the newest golden routed runs pinned from this recipe
    and that still exists; failing that, the recipe unpinned, which names a
    golden built before pinning existed.
    """
    if source and Path(source).exists():
        return pinning.settle(recipe, paths.root, driver,
                              lambda item: driver.golden_name(toolchain_of(item)),
                              source=source), source
    spec = golden_index.newest_pinned(paths, recipe, driver.exists)
    return (spec if spec is not None else dict(recipe)), None


def usable_source(driver, toolchain, source):
    """(source-or-None, problem-or-None) for building this target's golden.

    The default source is the client's cache, `<engine_root>/src/<repo>/latest`,
    and that exists only after the first routed run. A golden that is already
    built does not need it: `prepare` reuses the golden and every clone starts
    from the tree baked into it. A golden that is not built does, and failing
    later inside a tar injection says nothing about why, so say it here.
    """
    if source and Path(source).exists():
        return source, None
    name = driver.golden_name(toolchain)
    if driver.exists(name):
        return None, None
    if source:
        return None, ('%s is absent and %s is not built: the source cache is written by the '
                      'first routed run, so run one claimed command from an enrolled worktree '
                      'first, or pass --source <a tree on the worker>' % (source, name))
    return None, ('%s is not built and no --source was given to build it from' % name)


def run(root, *, journey=None, surfaces=None, source=None, hog_kind='file',
        floor_gib=4, quota_gib=1, keep=False, journey_argv=None, surfaces_argv=None,
        driver=None, targets=None):
    """Return the verdict dictionary. Never raises for a failed check.

    `targets` is the derived plan: one entry per distinct toolchain an enrolled
    repository names, each with its toolchain dictionary, its source, and the
    journey and surface checks its own jobs make possible. Without it, the
    explicit `journey` / `surfaces` toolchain files are the targets.
    """
    paths = Paths(root).ensure()
    driver = driver or IncusDriver(root=paths.root)
    checks = Checks()
    instances = []
    if targets is None:
        targets = legacy_targets(journey, surfaces, source, journey_argv, surfaces_argv)
    budget = BUDGET_SECONDS * max(1, len(targets))

    def clone(toolchain, tag, limits, src):
        golden = driver.prepare(toolchain, source=src, log=lambda text: None)
        checks.add('golden %s ready' % golden.name, bool(golden.name),
                   'reused' if golden.reused else 'built in %.1fs' % golden.built_seconds)
        instance = driver.clone(golden, tag, limits=limits)
        instances.append(instance)
        checks.add('clone %s under 2s' % tag, instance.clone_seconds < 2.0,
                   '%.2fs clone, %.2fs start' % (instance.clone_seconds, instance.start_seconds))
        driver.harden(instance, limits)
        return golden, instance

    # --- the machine itself -------------------------------------------------
    code, out, _ = driver.incus('project', 'list', '--format', 'csv', check=False)
    checks.add('project %s exists' % driver.project, driver.project in out)
    room = driver.capacity(floor_gib=floor_gib)
    checks.add('pool headroom above the floor', room.get('ok'),
               '%.2f GiB free of %.1f, floor %d GiB'
               % (room.get('free_gib', 0), room.get('total_bytes', 0) / (1 << 30), floor_gib))
    if not targets:
        checks.add('a toolchain to prove', False,
                   'no enrolled repository names a [worker] toolchain and no --journey or '
                   '--surfaces file was given, so no golden was cloned')

    proven = []
    for position, target in enumerate(targets):
        label = target.get('label') or 'target %d' % (position + 1)
        suffix = '' if position == 0 else '-%d' % (position + 1)
        try:
            toolchain_of(target['toolchain'])
            spec, pinned_from = pinned_target(paths, driver, target['toolchain'],
                                              target.get('source'))
            toolchain = toolchain_of(spec)
        except (KeyError, TypeError) as error:
            checks.add('%s toolchain readable' % label, False, 'missing %s' % error)
            continue
        checks.add('%s golden %s pinned' % (label, driver.golden_name(toolchain)), True,
                   '%s, from %s' % (pinning.describe(spec),
                                    pinned_from or 'the newest golden routed runs used'))
        src, problem = usable_source(driver, toolchain, target.get('source'))
        if problem:
            checks.add('%s source for %s' % (label, driver.golden_name(toolchain)), False, problem)
            continue
        proven.append(toolchain)
        for note in target.get('notes') or ():
            checks.add('%s: %s' % (label, note), True, 'not a failure; nothing to run')
        if target.get('journey'):
            journey_check(driver, checks, clone, instances, label, toolchain, src,
                          target['journey'], 'canary-journey' + suffix,
                          prepare_command=(target.get('toolchain') or {}).get(
                              'prepare_command') or '',
                          cache_root=paths.root / 'turbo-cache')
        if target.get('surface'):
            surface_check(driver, checks, clone, instances, label, toolchain, src,
                          target['surface'], 'canary-surfaces' + suffix)

    # --- runs cannot reach each other ---------------------------------------
    if proven:
        isolation_check(driver, checks, clone, instances, proven[0],
                        cache_root=paths.root / 'turbo-cache')

    # --- the disk quota -----------------------------------------------------
    # A quota that is set and not enforced is worse than no quota: the pool
    # fills anyway and the operator believes it cannot.
    if proven:
        toolchain = proven[0]
        try:
            # The quota limits *referenced* bytes, so it is sized against the
            # golden this clone shares extents with rather than against a
            # constant. See `IncusDriver.quota`.
            base = driver.volume_bytes(driver.golden_name(toolchain)) / (1 << 30)
            size = int(base) + 1 + quota_gib
            limits = Limits(memory_mib=512, ceiling_mib=1024, cpus_hint=1,
                            wall_seconds=180, disk_gib=size)
            _, instance = clone(toolchain, 'canary-quota', limits, None)
            megabytes = (quota_gib + 1) * 1024
            _, out, _ = driver.sh(
                instance.name,
                'dd if=/dev/zero of=/work/.pandora-quota-probe bs=1M count=%d 2>&1 | tail -1; '
                'rm -f /work/.pandora-quota-probe' % megabytes,
                check=False, timeout=600)
            refused = any(word in out.lower() for word in
                          ('no space', 'quota exceeded', 'disk quota', 'error writing'))
            checks.add('disk quota refuses an over-limit write', refused,
                       '%d GiB quota over a %.2f GiB golden, wrote %d MiB: %s'
                       % (size, base, megabytes, out.strip()[:140]))
            checks.add(*receipt_of(driver, instance, instances))
        except Exception as error:                                   # noqa: BLE001
            checks.add('disk quota enforced', False, '%s: %s' % (type(error).__name__, error))

    # --- the memory watchdog ------------------------------------------------
    # The first proven golden, in enrollment order, as for the quota. Either
    # golden would do: the hog writes its own working set (see `memtest`), so
    # the verdict does not depend on what the repository put in /work.
    if proven:
        toolchain = proven[0]
        limits = Limits(memory_mib=512, ceiling_mib=512, cpus_hint=1, wall_seconds=120)
        try:
            _, instance = clone(toolchain, 'canary-oom', limits, None)
            mark = time.monotonic()
            result = driver.execute(instance, hog(hog_kind), env={}, cwd='/work', limits=limits)
            elapsed = time.monotonic() - mark
            evidence = {key: value for key, value in result.evidence.items() if key != 'samples'}
            # The default hog is a file-cache overrun; another `--hog` says its own name.
            checks.add('file-cache thrash is killed as oom' if hog_kind == 'file'
                       else '%s memory hog is killed as oom' % hog_kind,
                       result.outcome == 'oom',
                       json.dumps(evidence)[:300])
            checks.add('oom verdict inside 60s', elapsed < 60, '%.1fs' % elapsed)
            checks.add('oom verdict carries evidence',
                       bool(evidence.get('reason')) and bool(evidence.get('events')),
                       evidence.get('reason', ''))
            checks.add(*receipt_of(driver, instance, instances))
        except Exception as error:                                   # noqa: BLE001
            checks.add('memory watchdog works', False, '%s: %s' % (type(error).__name__, error))

    if not keep:
        for instance in instances:
            driver.incus('delete', '-f', instance.name, check=False)

    # One budget per toolchain: two repositories on one worker are two
    # goldens to clone and two journeys to run, and a gate that fails because
    # more was enrolled is measuring the enrollment, not the machine.
    checks.add('canary inside its %ds budget' % budget,
               checks.seconds < budget, '%.1fs' % checks.seconds)
    failures = checks.failures
    return {'ok': not failures, 'checks': checks.rows, 'failures': len(failures),
            'seconds': checks.seconds,
            'targets': [{'label': target.get('label'), 'source': target.get('source'),
                         'journey': (target.get('journey') or {}).get('id'),
                         'surface': (target.get('surface') or {}).get('id')}
                        for target in targets],
            'reason': '; '.join('%s: %s' % (row['check'], row['detail'] or 'false')
                                for row in failures) or None}


def journey_check(driver, checks, clone, instances, label, toolchain, source, spec, tag,
                  prepare_command='', cache_root=None):
    """Docker, the compose stack if one is named, and one real journey.

    A `[worker]` table with a `prepare_command` gets it run first, exactly as
    the runner runs it in every routed clone: `bash -c` in /work with the job's
    environment plus the turbo cache's. A journey that passes only because the
    canary skipped the step every real run depends on proves nothing (#88).
    """
    # No explicit quota: this clone takes the worker's own per-run default,
    # which is the number a real run gets, so the canary proves that path.
    limits = Limits(memory_mib=3800, ceiling_mib=5120, cpus_hint=2, wall_seconds=600)
    try:
        _, instance = clone(toolchain, tag, limits, source)
        # `systemctl start docker` is retried rather than asserted: the
        # instance is ready as soon as /run/systemd/system exists, which is
        # early enough that docker.service may not be loaded yet, and a
        # single `&&` chain then short-circuits into an empty answer.
        _, docker, err = driver.sh(
            instance.name,
            'for i in $(seq 100); do systemctl start docker >/dev/null 2>&1 && break; '
            'sleep 0.3; done\n'
            'for i in $(seq 150); do docker info >/dev/null 2>&1 && break; sleep 0.2; done\n'
            'docker info --format "{{.Driver}} {{.CgroupVersion}}"',
            check=False, timeout=300)
        checks.add('nested dockerd up', 'overlay' in docker, docker.strip() or err.strip()[:160])
        if prepare_command:
            env = dict(spec.get('env') or {})
            if cache_root is not None:
                cache_env, _ = turbocache.env_for(cache_root, label)
                for key, value in cache_env.items():
                    env.setdefault(key, value)
            prep = driver.execute(instance, ['bash', '-c', prepare_command], env=env,
                                  cwd='/work', limits=limits)
            if not checks.add('%s prepare_command in %ds' % (label, round(prep.seconds)),
                              prep.outcome == 'ok' and prep.exit_code == 0,
                              'outcome=%s exit=%s in %.1fs' % (prep.outcome, prep.exit_code,
                                                               prep.seconds)):
                # The journey would fail for the same reason, one row later and
                # with less to say about why.
                checks.add(*receipt_of(driver, instance, instances))
                return
        compose = spec.get('compose')
        if compose:
            _, stack, _ = driver.sh(
                instance.name,
                'cd %s && docker compose -f %s up -d --wait 2>&1 | tail -2; '
                'docker ps --format "{{.Names}}" | wc -l'
                % (shlex.quote(spec.get('cwd') or '/work'), shlex.quote(compose)),
                check=False, timeout=420)
            count = stack.strip().splitlines()[-1] if stack.strip() else '0'
            checks.add('compose stack up', count.isdigit() and int(count) > 0,
                       '%s containers' % count)
            driver.sh(instance.name,
                      'cd %s && docker compose -f %s down -v 2>&1 | tail -1'
                      % (shlex.quote(spec.get('cwd') or '/work'), shlex.quote(compose)),
                      check=False, timeout=420)
            _, left, _ = driver.sh(instance.name, 'docker ps -q | wc -l', check=False)
            checks.add('compose stack down', left.strip() == '0',
                       '%s containers left' % left.strip())
        result = driver.execute(instance, list(spec['argv']), env=dict(spec.get('env') or {}),
                                cwd=spec.get('cwd') or '/work', limits=limits)
        checks.add('%s journey %s passes' % (label, spec.get('id')),
                   result.outcome == 'ok' and result.exit_code == 0,
                   'outcome=%s exit=%s in %.1fs' % (result.outcome, result.exit_code,
                                                    result.seconds))
        peak = result.usage.memory_peak
        checks.add('run crossed its soft limit without being killed',
                   result.outcome == 'ok' and peak > limits.memory_mib * 1048576 * 0.5,
                   'peak %d MiB, reservation %d, ceiling %d'
                   % (peak // 1048576, limits.memory_mib, limits.ceiling_mib))
        checks.add('run stayed under its ceiling', peak < limits.ceiling_mib * 1048576,
                   'peak %d MiB of %d' % (peak // 1048576, limits.ceiling_mib))
        checks.add(*receipt_of(driver, instance, instances))
    except Exception as error:                                       # noqa: BLE001
        checks.add('%s journey golden usable' % label, False,
                   '%s: %s' % (type(error).__name__, error))


PROBE_PORT = 8172
LISTEN_SECONDS = 15
# Incus runs dnsmasq on the bridge address and opens 53 to every port on it,
# so it is the one thing on the bridge a run can always reach.
BRIDGE_DNS_PORT = 53


def connects(host, port, timeout=2.0):
    """True when this host opens a TCP connection to host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe(driver, name, host, port, seconds=2):
    """'open' or 'closed': a TCP connect from inside one instance, with a deadline."""
    _, out, _ = driver.sh(
        name, "timeout %d bash -c 'exec 3<>/dev/tcp/%s/%d' 2>/dev/null && echo open || echo closed"
        % (seconds, host, port), check=False, timeout=30)
    return 'open' if out.strip().endswith('open') else 'closed'


def address_of(driver, name, ready=lambda address: True):
    """The instance's eth0 address once `ready` accepts it, or the last one seen."""
    deadline = time.monotonic() + LISTEN_SECONDS
    while True:
        address = driver.address(name)
        if (address and ready(address)) or time.monotonic() >= deadline:
            return address
        time.sleep(0.2)


def bridge_target(cache_root):
    """(label, host, port) of something on the bridge address an isolated run must reach.

    The turbo cache when it is serving, because that is the reach isolation
    must not cost. Otherwise the bridge's own DNS.
    """
    endpoint, _ = turbocache.env_for(cache_root, 'canary') if cache_root else ({}, '')
    if endpoint:
        host, port = endpoint['TURBO_API'].split('//', 1)[1].split('/', 1)[0].rsplit(':', 1)
        return 'the turbo cache', host, int(port)
    try:
        host = turbocache.bridge_address(turbocache.DEFAULT_BRIDGE)
    except OSError:
        host = None
    return 'the bridge DNS', host, BRIDGE_DNS_PORT


def isolation_check(driver, checks, clone, instances, toolchain, cache_root=None):
    """Two runs at once, and neither can reach a port the other opened (#172).

    A refused connect proves isolation only when both ends work, so two
    positive rows come first. A listens on 0.0.0.0, as a postgres or an API
    under test does, and the host must reach it: the port is open. B must
    reach the bridge address (the turbo cache when it serves, else the
    bridge's DNS): B has a network, and isolation has not cut runs off from
    the cache. Only then must B's connect to A fail.
    """
    limits = Limits(memory_mib=256, ceiling_mib=512, cpus_hint=1, wall_seconds=60)
    try:
        _, a = clone(toolchain, 'canary-net-a', limits, None)
        _, b = clone(toolchain, 'canary-net-b', limits, None)
        _, out, _ = driver.sh(
            a.name,
            'if command -v python3 >/dev/null; then '
            'setsid python3 -m http.server %d --bind 0.0.0.0 </dev/null >/dev/null 2>&1 & '
            'elif command -v node >/dev/null; then '
            'setsid node -e "require(\'net\').createServer(s => s.end()).listen(%d)" '
            '</dev/null >/dev/null 2>&1 & '
            'else echo "no python3 or node to listen with"; fi' % (PROBE_PORT, PROBE_PORT),
            check=False, timeout=30)
        address = address_of(driver, a.name,
                             lambda found: connects(found, PROBE_PORT, timeout=1.0))
        listening = checks.add('a run port answers the host', bool(address) and
                               connects(address, PROBE_PORT),
                               '%s:%d %s' % (address or 'no eth0 address', PROBE_PORT,
                                             out.strip()[:120]))
        label, host, port = bridge_target(cache_root)
        theirs = address_of(driver, b.name)
        got = probe(driver, b.name, host, port) if theirs and host else 'not probed'
        networked = checks.add('an isolated run reaches %s' % label, got == 'open',
                               '%s (%s) -> %s:%s %s' % (b.name, theirs or 'no eth0 address',
                                                        host or 'no bridge address', port, got))
        if listening and networked:
            got = probe(driver, b.name, address, PROBE_PORT)
            checks.add('runs cannot reach each other on the bridge', got == 'closed',
                       '%s -> %s:%d %s' % (b.name, address, PROBE_PORT, got))
        checks.add(*receipt_of(driver, a, instances))
        checks.add(*receipt_of(driver, b, instances))
    except Exception as error:                                       # noqa: BLE001
        checks.add('runs cannot reach each other on the bridge', False,
                   '%s: %s' % (type(error).__name__, error))


def surface_check(driver, checks, clone, instances, label, toolchain, source, spec, tag):
    """The surface job's cheap step -- `validate`, or a one-shard `plan` -- not a browser."""
    limits = Limits(memory_mib=3800, ceiling_mib=6144, cpus_hint=2, wall_seconds=300)
    try:
        _, instance = clone(toolchain, tag, limits, source)
        result = driver.execute(instance, list(spec['argv']), env=dict(spec.get('env') or {}),
                                cwd=spec.get('cwd') or '/work', limits=limits)
        checks.add('%s surface %s %s answers' % (label, spec.get('id'), spec.get('step', 'plan')),
                   result.exit_code == 0,
                   'outcome=%s exit=%s in %.1fs' % (result.outcome, result.exit_code,
                                                    result.seconds))
        checks.add(*receipt_of(driver, instance, instances))
    except Exception as error:                                       # noqa: BLE001
        checks.add('%s surface golden usable' % label, False,
                   '%s: %s' % (type(error).__name__, error))


def receipt_of(driver, instance, instances):
    """(name, ok, detail) for destroying one canary instance."""
    try:
        got = driver.destroy(instance)
        instances.remove(instance)
        return ('%s destroy receipt clean' % instance.name, got.clean,
                '%.2fs, leftovers=%s' % (got.seconds, list(got.leftovers)))
    except DestroyIncomplete as error:
        return ('%s destroy receipt clean' % instance.name, False,
                json.dumps(error.receipt, default=str)[:300])
    except Exception as error:                                       # noqa: BLE001
        return ('%s destroy receipt clean' % instance.name, False, str(error)[:200])
