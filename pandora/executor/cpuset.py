"""Which host CPUs a run is pinned to, on physical-core boundaries (#201).

`limits.cpu=<count>` let Incus choose the host threads. On an SMT host those
can be sibling pairs, so a run told `PANDORA_CPUS=8` may hold four physical
cores, and a CPU-bound job that sizes its parallelism from the number pays
memory for no speed. Here the engine reads the host's sibling map, gives each
run whole cores (both threads of a core go to the same run), spreads runs over
the least-used cores, and pins the result as an explicit `limits.cpu` list.

The pin's width stays a thread count: it is what `nproc` reports inside the
run. `PANDORA_CPUS` becomes the number of physical cores the cpuset touches,
which is the parallelism a CPU-bound job can expect. On a host without SMT the
two numbers are the same.

Everything but `host_topology` is pure, so it is tested with a fake sibling map.
When the topology cannot be read, the caller keeps the count pin it had.
"""
from collections import Counter
from pathlib import Path

SYS_CPU = Path('/sys/devices/system/cpu')


def parse_list(text):
    """A kernel or Incus CPU list, `0-3,8,10-11`, as a sorted list of ints.

    Raises ValueError on anything that is not a list of numbers and ranges.
    """
    cpus = set()
    for part in (text or '').strip().split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            low, high = (int(item) for item in part.split('-', 1))
            if high < low or low < 0:
                raise ValueError('bad CPU range %r' % part)
            cpus.update(range(low, high + 1))
        else:
            value = int(part)
            if value < 0:
                raise ValueError('bad CPU %r' % part)
            cpus.add(value)
    return sorted(cpus)


def format_list(cpus):
    """The inverse of `parse_list`, as compact ranges: `0-3,16-19`.

    A lone CPU is written `5-5`, because Incus reads a bare number in
    `limits.cpu` as a count, not as a CPU.
    """
    ordered = sorted(set(cpus))
    if not ordered:
        raise ValueError('an empty cpuset')
    ranges, start, last = [], ordered[0], ordered[0]
    for cpu in ordered[1:]:
        if cpu == last + 1:
            last = cpu
            continue
        ranges.append((start, last))
        start = last = cpu
    ranges.append((start, last))
    if len(ranges) == 1 and start == last:
        return '%d-%d' % (start, start)
    return ','.join('%d' % low if low == high else '%d-%d' % (low, high)
                    for low, high in ranges)


def cores_of(siblings, online=None):
    """Physical cores, each a sorted tuple of its threads, ordered by first thread.

    `siblings` maps a CPU to its `thread_siblings_list` text. Only CPUs in
    `online` count when it is given. A map in which two CPUs disagree about who
    shares a core raises ValueError: a wrong topology must fall back, not pin.
    """
    online = set(online) if online is not None else None
    owner, cores = {}, set()
    for cpu, text in siblings.items():
        if online is not None and cpu not in online:
            continue
        group = {item for item in parse_list(text)
                 if online is None or item in online} | {cpu}
        core = tuple(sorted(group))
        for item in core:
            if owner.setdefault(item, core) != core:
                raise ValueError('CPU %d is in two cores: %s and %s'
                                 % (item, owner[item], core))
        cores.add(core)
    missing = set(owner) - set(siblings)
    if missing:
        raise ValueError('CPUs %s are named as siblings but not listed'
                         % format_list(missing))
    return tuple(sorted(cores))


def host_topology(root=SYS_CPU):
    """This host's physical cores from sysfs, or None when it cannot be read.

    None means "pin by count, as before": a non-Linux engine host, a container
    without sysfs, or a sibling map that does not add up.
    """
    root = Path(root)
    try:
        online = parse_list((root / 'online').read_text())
    except (OSError, ValueError):
        return None
    siblings = {}
    for cpu in online:
        topology = root / ('cpu%d' % cpu) / 'topology'
        for name in ('thread_siblings_list', 'core_cpus_list'):
            try:
                siblings[cpu] = (topology / name).read_text()
                break
            except OSError:
                continue
        else:
            return None
    try:
        cores = cores_of(siblings, online)
    except ValueError:
        return None
    return cores or None


def allocate(cores, width, held=()):
    """The CPUs for one run: whole cores, least used first, `width` threads or more.

    `held` is the cpusets of the runs already pinned. A core's load is the mean
    number of held cpusets per thread, and ties go to the lowest core, so four
    runs of four cores each on a sixteen-core host get disjoint cores, and a
    fifth starts sharing the least-used ones. The width rounds up to whole
    cores: 7 threads on a two-thread-per-core host is 4 cores and 8 threads,
    and 1 thread is 1 core and 2 threads. A run never holds half a core, so
    every core it is told about in `PANDORA_CPUS` is a whole one, and `nproc`
    may exceed the configured width by less than one core. The width is
    clamped to the host.
    """
    if not cores:
        raise ValueError('no cores to pin')
    total = sum(len(core) for core in cores)
    width = max(1, min(int(width), total))
    load = Counter(cpu for cpuset in held for cpu in cpuset)
    order = sorted(range(len(cores)),
                   key=lambda index: (sum(load[cpu] for cpu in cores[index])
                                      / float(len(cores[index])), index))
    chosen = []
    for index in order:
        if len(chosen) >= width:
            break
        chosen.extend(cores[index])
    return sorted(chosen)


def physical_cores(cores, cpus):
    """How many whole physical cores a cpuset holds: the run's `PANDORA_CPUS`."""
    cpus = set(cpus)
    return sum(1 for core in cores if cpus.issuperset(core))


def rebalance(cores, placed, apply=None):
    """Moves that spread live runs back onto idle cores. {key: new cpus}.

    A pin is chosen when a run starts, so runs that started while the host was
    full keep sharing cores after their neighbors finish. `placed` is the live
    runs' (key, cpus) in admission order. Only a run that shares a core with
    another live run moves, the last placed (most recently admitted) first,
    and only onto cores no live run touches, keeping its thread and core
    counts so its `PANDORA_CPUS` stays true. A run with nowhere idle enough to
    go stays where it is.

    `apply(key, cpus)` makes one move and returns whether it happened. A move
    it refuses (a run not cloned yet, a repin that failed) leaves that run on
    its old cores for the rest of the plan, so an older run that still shares
    them can move instead. Without `apply` every planned move counts.
    """
    core_of = {cpu: index for index, core in enumerate(cores) for cpu in core}
    users = Counter()
    for _, cpus in placed:
        users.update({core_of[cpu] for cpu in cpus if cpu in core_of})
    moves = {}
    for key, cpus in reversed(list(placed)):
        mine = {core_of[cpu] for cpu in cpus if cpu in core_of}
        if not any(users[index] > 1 for index in mine):
            continue
        idle = [cores[index] for index in range(len(cores)) if users[index] == 0]
        if sum(len(core) for core in idle) < len(cpus):
            continue
        chosen = allocate(idle, len(cpus))
        if (len(chosen) != len(cpus)
                or physical_cores(cores, chosen) != physical_cores(cores, cpus)):
            continue
        if apply is not None and not apply(key, chosen):
            continue
        for index in mine:
            users[index] -= 1
        users.update({core_of[cpu] for cpu in chosen})
        moves[key] = chosen
    return moves


def describe(cores):
    """`16 core(s), 32 thread(s)`, for status lines."""
    return '%d core(s), %d thread(s)' % (len(cores), sum(len(core) for core in cores))
