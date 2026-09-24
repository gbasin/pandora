"""Enrollment: which worktrees the shim may claim, decided without running git.

Each worktree's `pandora.toml` is the truth about what that worktree claims.
The shim cannot read TOML without a Python start, so the daemon derives a flat
**claim cache** from it, per worktree, and the shim reads that with the shell's
own ``read`` builtin. Like direnv and mise, the cache is keyed by freshness: the
shim trusts it only while it is not older than the file it was derived from,
and otherwise takes the slow path, which asks the daemon and rewrites it.

Three files, all derived, all safe to delete:

``<worktree git dir>/pandora-claims``
    The claim cache. ``git rev-parse --git-dir`` of the worktree:
    ``<common>/worktrees/<name>/`` for a linked worktree, ``<common>/`` for the
    main one. Written by the daemon whenever it classifies from that worktree.
``<common>/pandora-repo``
    Registration: this repository is enrolled. It holds only the socket and the
    client home, so a worktree created tomorrow, with no cache yet, takes the
    slow path instead of the non-enrolled exec. Written by `pandora enroll`,
    and by the daemon on first contact.
``<common>/pandora-enrolled``
    The v0.2 marker, one per repository, written by `pandora enroll` before
    caches existed. Still read, for one release, by a worktree that has no
    cache while there is no registration file; `pandora enroll` replaces it.

The line format is the same in all three, and the claim list can change (the
daemon rewrites it) with no shim change.
"""
import hashlib
import os
import threading
import time
from pathlib import Path

MARKER = 'pandora-enrolled'       # the v0.2 per-repository marker, read for one release
CACHE = 'pandora-claims'           # per worktree, in the worktree's own git dir
REGISTRATION = 'pandora-repo'      # per repository, in the common dir
# A source edited this recently may share its second with the cache written
# from it, and macOS's /bin/sh (bash 3.2) compares `-nt` in whole seconds. Such
# a cache is dated this far back, so the shim takes the slow path again until
# the edit has settled, and the daemon then dates it exactly.
SETTLE_NS = 2_000_000_000


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
    which is what enrollment is a property of. This one identifies the checkout,
    which is what a job runs in and what a subdirectory invocation is measured
    against.
    """
    here = Path(start).resolve()
    for directory in [here, *here.parents]:
        dot = directory / '.git'
        if dot.is_dir() or dot.is_file():
            return str(directory)
    return None


def git_dir(start):
    """The git dir of the worktree `start` is inside, or None: where its cache lives.

    `git rev-parse --git-dir`, by the shim's own walk: the `.git` directory of
    the main worktree, or the `gitdir:` target of a linked one, before its
    `/worktrees/<name>` tail is stripped.
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
            return str(target)
    return None


def cache_path(start):
    """This worktree's claim cache, or None outside a repository."""
    found = git_dir(start)
    return Path(found) / CACHE if found else None


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
           policies=(), subdirectory=None, derived=None, config=None, digest=None,
           client=None, noclient=None,
           header='# pandora enrollment v1 -- written by pandora enroll, safe to delete'):
    """The marker text.  One directive per line, first word is the key.

    `home` is the directory the `pandora` package lives in, so a shim installed
    anywhere on PATH can find the client without an absolute path baked into it.

    `policy` lines carry each claimed form's size class and declared fallback.
    The POSIX shim never reads them -- its `case` matches six keys (`sock`,
    `home`, `subdirectory`, `strip`, `claim`, `heavy`) and ignores the rest --
    but the Python client does, and it is the only thing that can answer "may
    this run here" when the daemon that owns the configuration is the thing
    that is gone.

    `subdirectory` is `[matching] subdirectory`. The shim acts only on
    `passthrough`: below the worktree root it claims nothing, decided by
    comparing `$PWD` with the root it already walked to, so no fork.

    Every `strip` line is written before any `claim` or `heavy` line. The shim
    strips as it reads, in one pass, so this order is part of the format.

    A claim cache adds the lines its freshness is judged by, all read by the
    shim with builtins. `derived` says which file the claims came from: `own`,
    the worktree's `pandora.toml` (never written as a path, which could not be
    compared with the shim's `$PWD` spelling of the root); `external`, the
    `[[repos]] config` fallback; or `none`, neither exists. `config` names that
    fallback whenever `[[repos]]` has one, present or not. `client` names the
    client configuration, or `noclient` names where it would be when there is
    none. The shim treats the cache as stale when a file it was derived from
    is newer, has gone, or has appeared (see `cache_state`). `digest` is the
    derived file's content hash, which `pandora doctor` compares, for a file
    replaced by one with an older mtime.
    """
    lines = [header, 'sock ' + socket_path, 'repo ' + repo]
    if home:
        lines.append('home ' + home)
    if origin:
        lines.append('origin ' + origin)
    if derived:
        lines.append('derived ' + derived)
    if config:
        lines.append('config ' + config)
    if digest:
        lines.append('digest ' + digest)
    if client:
        lines.append('client ' + client)
    if noclient:
        lines.append('noclient ' + noclient)
    if subdirectory:
        lines.append('subdirectory ' + subdirectory)
    lines += ['strip ' + ' '.join(prefix) for prefix in strip_prefixes]
    lines += ['claim ' + ' '.join(claim) for claim in claims]
    lines += ['policy %s %s %d %s' % (item['size'], item['fallback'],
                                      1 if item.get('writeback') else 0,
                                      ' '.join(item['prefix']))
              for item in policies]
    lines += ['heavy ' + ' '.join(item) for item in heavy]
    return '\n'.join(lines) + '\n'


SCALARS = ('sock', 'repo', 'origin', 'home', 'subdirectory', 'derived', 'config', 'digest',
           'client', 'noclient')


def parse(text):
    marker = dict({key: None for key in SCALARS},
                  strip=[], claim=[], heavy=[], policy=[])
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, _, rest = line.partition(' ')
        if key in SCALARS:
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


def temporary(path):
    """A temp name no other writer shares: the daemon writes from many threads."""
    return path.with_name('%s.%d.%d.tmp' % (path.name, os.getpid(), threading.get_ident()))


def write(common, text, name=MARKER):
    path = Path(common) / name
    temp = temporary(path)
    temp.write_text(text)
    temp.replace(path)
    return path


def digest_of(path):
    """A short content hash of one config file, or None when it cannot be read."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def cache_text(repo_config, *, socket_path, repo, derived, external=None, digest_path=None,
               client=None, home=None, why=None):
    """The claim cache for one worktree, derived from that worktree's config.

    `derived` is `own`, `external` or `none` (see `render`), `external` the
    `[[repos]] config` path when there is one, `digest_path` the file the claims
    came from, and `client` the client configuration's path, whether or not it
    exists. `repo_config` None means the worktree has no usable configuration,
    or its repository has no `[[repos]]` entry (`why` says which): the cache
    then claims nothing, so the shim execs every command with no Python start,
    and it goes stale like any other the moment a file it names changes.
    """
    from ..config import classify
    header = '# pandora claim cache v1 -- written by the daemon, safe to delete'
    if why:
        header += '\n# claims nothing: ' + why.replace('\n', ' ')
    present = bool(client) and Path(client).is_file()
    common = dict(socket_path=socket_path, repo=repo, home=home, derived=derived,
                  config=str(external) if external else None,
                  digest=digest_of(digest_path) if digest_path else None,
                  client=str(client) if present else None,
                  noclient=str(client) if client and not present else None, header=header)
    if repo_config is None:
        return render(claims=[], heavy=heavy_forms([]), **common)
    claims = classify.claim_index(repo_config)
    return render(claims=claims, heavy=heavy_forms(claims),
                  policies=classify.policy_index(repo_config),
                  strip_prefixes=repo_config['matching']['strip_prefixes'],
                  subdirectory=repo_config['matching']['subdirectory'], **common)


def repo_entry(config, cwd):
    """The client config's `[[repos]]` entry for the repository `cwd` is in, or None.

    By path first, then by git common dir, because worktrees usually live
    beside the enrolled root rather than inside it.
    """
    from . import settings
    found = settings.enrollment_for(config, cwd)
    if found is not None:
        return found
    common = common_dir(cwd)
    if common is None:
        return None
    for repo in config['repos']:
        try:
            if common_dir(repo['root']) == common:
                return repo
        except OSError:
            continue
    return None


def owner_of(common):
    """The socket the registration (else the v0.2 marker) names, or None when neither exists.

    Only the daemon on that socket, or a client acting for it, writes caches:
    another daemon asked about this worktree must not point it at itself.
    """
    for name in (REGISTRATION, MARKER):
        try:
            return parse((Path(common) / name).read_text()).get('sock') or ''
        except OSError:
            continue
    return None


def write_owned(root, text, sources, socket_path):
    """Write this worktree's cache if its repository is enrolled with `socket_path`.

    Returns (written, why not). Never raises OSError.
    """
    cache, common = cache_path(root), common_dir(root)
    if cache is None or common is None:
        return False, 'not inside a repository'
    owner = owner_of(common)
    if owner is None:
        return False, 'not enrolled'
    if not owner or os.path.realpath(owner) != os.path.realpath(str(socket_path)):
        return False, 'the repository is enrolled with %s, not %s' % (owner, socket_path)
    try:
        return write_cache(cache, text, sources), None
    except OSError as error:
        return False, 'cannot write %s: %s' % (cache, error)


def derive(root, repo, *, socket_path, client, home=None, load=None, why=None):
    """(cache text, the files it depends on) for one worktree and its `[[repos]]` entry.

    One derivation for the daemon, `pandora enroll` and the client with no
    daemon, so all three write the same text. `load(root, repo)` lets the
    daemon use its parsed-config cache; by default the file is loaded here.
    """
    from ..config import loader
    from ..errors import ConfigError
    own = Path(root) / loader.FILENAME
    external = (repo or {}).get('config') or None
    config = None
    try:
        path, origin = loader.resolve(root, external)
        derived = 'own' if origin == 'repo-root' else 'external'
    except ConfigError as error:
        path, derived = None, 'none'
        why = why or str(error)
    if repo is None:
        why = why or 'no [[repos]] entry for this repository'
    elif path is not None:
        try:
            config = load(root, repo) if load else loader.load(path)
        except (ConfigError, OSError) as error:
            why = str(error)
    text = cache_text(config, socket_path=socket_path, repo=(repo or {}).get('name') or '-',
                      derived=derived, external=external, digest_path=path,
                      client=client, home=home, why=None if config is not None else why)
    return text, [own, external, client]


def registration_text(*, socket_path, repo, home=None):
    """`pandora-repo`: enrolled, and where the daemon is; no claims of its own."""
    return render(socket_path=socket_path, repo=repo, claims=[], home=home,
                  header='# pandora registration v1 -- written by pandora enroll, '
                         'safe to delete')


def mtime_ns(path):
    try:
        return os.stat(path).st_mtime_ns
    except (OSError, TypeError):
        return None


def write_cache(path, text, sources, *, clock=time.time_ns):
    """Write one claim cache atomically, dated so the shim's `-nt` reads it right.

    Dated the newest source's mtime, never "now": a source edited after the
    daemon read it is then newer than the cache, whatever the order of the two
    writes. A source edited within `SETTLE_NS` is dated that much earlier, so
    the shim takes the slow path until the edit settles. Returns True when
    anything was written.
    """
    path = Path(path)
    stamps = [stamp for stamp in (mtime_ns(source) for source in sources if source)
              if stamp is not None]
    now = clock()
    want = max(stamps) if stamps else now
    if now - want < SETTLE_NS:
        want -= SETTLE_NS
    try:
        if path.read_text() == text and path.stat().st_mtime_ns == want:
            return False
    except OSError:
        pass
    temp = temporary(path)
    temp.write_text(text)
    os.utime(temp, ns=(want, want))
    temp.replace(path)
    return True


def newer(first, second):
    """The shell's `[ first -nt second ]`: first exists and second does not, or is older."""
    one = mtime_ns(first)
    if one is None:
        return False
    two = mtime_ns(second)
    return two is None or one > two


def cache_state(root, cache):
    """Is this worktree's claim cache fresh? (state, why, parsed, the shim sees it).

    `state` is `fresh`, `stale` or `missing`. The shim's rule, exactly, and then
    the one thing it cannot check: whether the derived file still hashes to
    the cache's `digest`. A cache stale only by digest is one the shim trusts,
    so only a claimed command, which reaches the daemon, rewrites it.
    """
    cache = Path(cache)
    try:
        parsed = parse(cache.read_text())
    except OSError:
        return 'missing', 'no claim cache at %s' % cache, None, True
    own = Path(root) / 'pandora.toml'
    config, derived = parsed.get('config'), parsed.get('derived')

    def stale(why):
        return 'stale', why, parsed, True
    if derived == 'own':
        if not own.is_file():
            return stale('it was derived from %s, which is gone' % own)
        if newer(own, cache):
            return stale('%s changed after the cache was written' % own)
    elif derived == 'external':
        if own.is_file():
            return stale('it was derived from %s, and %s now exists' % (config, own))
        if not config or not Path(config).is_file():
            return stale('it was derived from %s, which is gone' % config)
        if newer(config, cache):
            return stale('%s changed after the cache was written' % config)
    elif derived == 'none':
        if own.is_file():
            return stale('%s appeared after the cache was written' % own)
        if config and Path(config).is_file():
            return stale('%s appeared after the cache was written' % config)
    else:
        return stale('it does not say what it was derived from')
    client, noclient = parsed.get('client'), parsed.get('noclient')
    if client and (not Path(client).is_file() or newer(client, cache)):
        return stale('%s changed after the cache was written' % client)
    if noclient and Path(noclient).is_file():
        return stale('%s appeared after the cache was written' % noclient)
    source = str(own) if derived == 'own' else config
    if (derived != 'none' and parsed.get('digest')
            and digest_of(source) not in (None, parsed['digest'])):
        return ('stale', '%s has other content than the cache was derived from, and an '
                'older mtime, so the shim cannot tell' % source, parsed, False)
    return 'fresh', 'derived from %s' % (source if derived != 'none' else
                                         'no configuration'), parsed, True


def caches_of(common):
    """Every worktree of one repository: [(root or None, cache path)].

    Read from the common dir without running git: `<common>` itself for the
    main worktree, and each `worktrees/<name>/gitdir`, which holds the linked
    worktree's `.git` file path.
    """
    common = Path(common)
    found = [(str(common.parent) if common.name == '.git' else None, common / CACHE)]
    try:
        entries = sorted((common / 'worktrees').iterdir())
    except OSError:
        entries = []
    for entry in entries:
        try:
            pointer = (entry / 'gitdir').read_text().strip()
        except OSError:
            pointer = ''
        root = str(Path(pointer).parent) if pointer else None
        found.append((root, entry / CACHE))
    return found


def claims_nothing_here(cwd, marker):
    """True when the marker claims only at the worktree root and `cwd` is below it.

    The POSIX shim's rule, for a caller that did not come through it (`pandora
    run`). The daemon reaches the same answer from the configuration.
    """
    if not marker or marker.get('subdirectory') != 'passthrough':
        return False
    root = worktree_root(cwd)
    return root is not None and Path(cwd).resolve() != Path(root).resolve()


def source_for(cwd):
    """(common dir, the file the shim reads claims from here, kind), as the shim chooses.

    The worktree's cache, else the registration (no claims: the slow path),
    else the v0.2 marker, else nothing. `kind` is `cache`, `registration`,
    `marker` or None.
    """
    common = common_dir(cwd)
    if common is None:
        return None, None, None
    cache = cache_path(cwd)
    for path, kind in ((cache, 'cache'), (Path(common) / REGISTRATION, 'registration'),
                       (Path(common) / MARKER, 'marker')):
        if path is not None and path.is_file():
            return common, path, kind
    return common, None, None


def marker_for(cwd):
    """(common dir, the parsed claims this worktree routes by, or None).

    The cache when there is one, fresh or not: the daemon applies the current
    configuration anyway, and this is what the client consults only when there
    is no daemon to ask. Else the v0.2 marker. A registration claims nothing.
    """
    common, path, kind = source_for(cwd)
    if path is None or kind == 'registration':
        return common, None
    return common, parse(path.read_text())


def key_of(argv, strip_prefixes):
    """Normalize one argv tail for matching against claims of any length.

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
