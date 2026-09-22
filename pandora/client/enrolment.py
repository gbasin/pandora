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


def render(*, socket_path, repo, claims, heavy=(), strip_prefixes=(), origin=None, home=None):
    """The marker text.  One directive per line, first word is the key.

    `home` is the directory the `pandora` package lives in, so a shim installed
    anywhere on PATH can find the client without an absolute path baked into it.
    """
    lines = ['# pandora enrolment v1 -- written by pandora enrol, safe to delete',
             'sock ' + socket_path, 'repo ' + repo]
    if home:
        lines.append('home ' + home)
    if origin:
        lines.append('origin ' + origin)
    lines += ['strip ' + ' '.join(prefix) for prefix in strip_prefixes]
    lines += ['claim ' + ' '.join(claim) for claim in claims]
    lines += ['heavy ' + ' '.join(item) for item in heavy]
    return '\n'.join(lines) + '\n'


def parse(text):
    marker = {'sock': None, 'repo': None, 'origin': None, 'home': None,
              'strip': [], 'claim': [], 'heavy': []}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, _, rest = line.partition(' ')
        if key in ('sock', 'repo', 'origin', 'home'):
            marker[key] = rest.strip()
        elif key in ('strip', 'claim', 'heavy'):
            marker[key].append(rest.split())
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
    """Normalise one argv tail into the one- and two-token claim keys.

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


def heavy(argv, marker):
    rest = key_of(argv, marker['strip'])
    for item in marker['heavy']:
        if rest[:len(item)] == item:
            return True
    return False
