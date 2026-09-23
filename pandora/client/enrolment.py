"""Enrolment: which worktrees the shim may claim, decided without running git.

Enrolment is a property of a repository, not of a worktree.  Every worktree of
one repository shares one git *common directory*, so one marker file written
there enrols all of them at once -- the ~135 eichler worktrees on this machine
are covered by a single file, and a worktree created tomorrow is covered the
moment it is created.

The marker is deliberately a flat ``key value`` text file.  The shim reads it
with the shell's own ``read`` builtin, so the whole enrolled-path decision costs
no process at all, and the claim list can change (the daemon rewrites it) with
no shim change.
"""
from pathlib import Path

MARKER = 'pandora-enrolled'


def common_dir(start):
    """The git common directory above ``start``, or None.

    Mirrors, exactly, what the POSIX shim does: walk up for ``.git``; if it is a
    directory that is the common dir; if it is a *file* (every git worktree) read
    the ``gitdir:`` pointer and strip the ``/worktrees/<name>`` tail.
    """
    here = Path(start).resolve()
    for directory in [here, *here.parents]:
        dot = directory / '.git'
        if dot.is_dir():
            return str(dot)
        if dot.is_file():
            try:
                text = dot.read_text().strip()
            except OSError:
                return None
            if not text.startswith('gitdir:'):
                return None
            target = Path(text[len('gitdir:'):].strip())
            if not target.is_absolute():
                target = (directory / target).resolve()
            parts = target.parts
            if 'worktrees' in parts:
                target = Path(*parts[:parts.index('worktrees')])
            return str(target)
    return None


def worktree_root(start):
    """The worktree `start` is inside, or None.

    Not the same question as `common_dir`: that one identifies the *repository*,
    which is what enrolment is a property of. This one identifies the checkout,
    which is what a job runs in and what a subdirectory invocation is measured
    against.
    """
    here = Path(start).resolve()
    for directory in [here, *here.parents]:
        dot = directory / '.git'
        if dot.is_dir() or dot.is_file():
            return str(directory)
    return None


# Unclaimed forms that look heavy enough to be worth counting. The shim runs
# them locally, unchanged, and logs one line each, so `pandora stats` can say
# what routing declined. A claimed form wins: the shim checks `claim` first.
DEFAULT_HEAVY = (('build',), ('lint',), ('typecheck',), ('install',), ('i',), ('dev',),
                 ('test',), ('exec',))


def heavy_forms(claims, candidates=DEFAULT_HEAVY):
    """The heavy list less anything claimed, so a marker never says both."""
    claimed = {tuple(claim) for claim in claims}
    return [list(item) for item in candidates if tuple(item) not in claimed]


def render(*, socket_path, repo, claims, heavy=(), strip_prefixes=(), origin=None, home=None,
           policies=()):
    """The marker text.  One directive per line, first word is the key.

    `home` is the directory the `pandora` package lives in, so a shim installed
    anywhere on PATH can find the client without an absolute path baked into it.

    `policy` lines carry each claimed form's size class and declared fallback.
    The POSIX shim never reads them -- its `case` matches five keys (`sock`,
    `home`, `strip`, `claim`, `heavy`) and ignores the rest -- but the Python
    client does, and it is the only thing that can answer "may this run here"
    when the daemon that owns the configuration is the thing that is gone.

    Every `strip` line is written before any `claim` or `heavy` line. The shim
    strips as it reads, in one pass, so this order is part of the format.
    """
    lines = ['# pandora enrolment v1 -- written by pandora enrol, safe to delete',
             'sock ' + socket_path, 'repo ' + repo]
    if home:
        lines.append('home ' + home)
    if origin:
        lines.append('origin ' + origin)
    lines += ['strip ' + ' '.join(prefix) for prefix in strip_prefixes]
    lines += ['claim ' + ' '.join(claim) for claim in claims]
    lines += ['policy %s %s %d %s' % (item['size'], item['fallback'],
                                      1 if item.get('writeback') else 0,
                                      ' '.join(item['prefix']))
              for item in policies]
    lines += ['heavy ' + ' '.join(item) for item in heavy]
    return '\n'.join(lines) + '\n'


def parse(text):
    marker = {'sock': None, 'repo': None, 'origin': None, 'home': None,
              'strip': [], 'claim': [], 'heavy': [], 'policy': []}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, _, rest = line.partition(' ')
        if key in ('sock', 'repo', 'origin', 'home'):
            marker[key] = rest.strip()
        elif key in ('strip', 'claim', 'heavy'):
            marker[key].append(rest.split())
        elif key == 'policy':
            parts = rest.split()
            if len(parts) >= 4:
                marker['policy'].append({'size': parts[0], 'fallback': parts[1],
                                         'writeback': parts[2] == '1',
                                         'prefix': parts[3:]})
    return marker


def write(common, text):
    path = Path(common) / MARKER
    temp = path.with_suffix('.tmp')
    temp.write_text(text)
    temp.replace(path)
    return path


def marker_for(cwd):
    common = common_dir(cwd)
    if common is None:
        return None, None
    path = Path(common) / MARKER
    if not path.is_file():
        return common, None
    return common, parse(path.read_text())


def key_of(argv, strip_prefixes):
    """Normalise one argv tail for matching against claims of any length.

    ``argv`` excludes the tool name.  Declared wrapper prefixes (``pnpm run
    test:unit``) are removed once each, in declaration order, exactly as the
    configured classifier does.
    """
    rest = list(argv)
    for prefix in strip_prefixes:
        if rest[:len(prefix)] == prefix:
            rest = rest[len(prefix):]
    return rest


def claimed(argv, marker):
    rest = key_of(argv, marker['strip'])
    for claim in marker['claim']:
        if rest[:len(claim)] == claim:
            return True
    return False


def policy_for(argv, marker):
    """The longest claimed form matching this argv, and what it declares.

    None means the marker is older than this rule, which the caller must treat
    as "unknown", not as "fine": an unknown job is decided as if it were large.
    """
    rest = key_of(argv, marker.get('strip') or [])
    best = None
    for item in marker.get('policy') or []:
        prefix = item['prefix']
        if rest[:len(prefix)] == prefix and (best is None or len(prefix) > len(best['prefix'])):
            best = item
    return best


def heavy(argv, marker):
    rest = key_of(argv, marker['strip'])
    for item in marker['heavy']:
        if rest[:len(item)] == item:
            return True
    return False
