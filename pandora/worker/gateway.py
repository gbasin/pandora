#!/usr/bin/env python3
"""The forced command a teammate's key is pinned to on a shared worker.

`authorized_keys` pins each non-admin key to this script with the client's name
and the worker's roots:

    restrict,command="<root>/bin/gateway --name sterling --engine-root <er> --worker-root <wr>" ssh-ed25519 ...

It exists because the account is shared: the OS cannot tell Sterling's key from
the owner's, so the key decides who the caller is. The filter admits exactly
the wire shapes a Pandora client sends:

* `sh -c 'cd "$HOME" && pwd'`, the home probe;
* `sh -c 'cat <bundle>/pandora/.bundle 2>/dev/null || true'`, the bundle check;
* `python3 -c <script> [args]`, only for the fixed feed scripts a send uses,
  allowlisted by sha256 in `<engine_root>/feeds.allow` and in every installed
  bundle's `pandora/.feeds`;
* `cd <bundle> && PYTHONPATH=<bundle> python3 -m pandora.engine.service ...`
  for the lifecycle verbs, and `pandora.worker.service` for read-only verbs;
* `rsync --server ...`, with every path argument inside the engine root.

On an admitted command the caller's name is set as PANDORA_GATEWAY_CLIENT and
the command runs unchanged, so the engine trusts the pin over whatever the
request claimed. Everything else is refused on stderr with exit 1.

The boundary is command shape, not content: a bundle is client code running as
the worker user, so a teammate who can ship a bundle can run anything one. The
gateway buys identity, revocation and verb scoping for the trusted-few model --
isolation between users is out of scope by design.

Standalone on purpose: this file must run before any bundle exists, so it
imports nothing from the package it guards.
"""
import argparse
import hashlib
import os
import re
import shlex
import sys

# Engine verbs a teammate may call. Admin-only on a user key: `retain`,
# `cache-clear`, `canary` (each mutates the worker). `supervise` is the
# engine's own spawn; it never legitimately arrives over SSH.
USER_ENGINE = frozenset((
    'submit', 'resubmit', 'lookup', 'status', 'result', 'cancel', 'wait',
    'logs', 'ps', 'stats', 'health', 'cache-stats', 'reconcile',
))
# `pandora.worker.service` verbs a teammate may call: the read-only survey.
# `gc`, `canary` and `ready` change the worker and stay admin-only.
USER_WORKER = frozenset(('status', 'capacity', 'goldens', 'pins'))

MODULES = {'pandora.engine.service': USER_ENGINE,
           'pandora.worker.service': USER_WORKER}

HOME_PROBE = 'cd "$HOME" && pwd'
# The bundle presence check, `bundle.ensure`'s only `sh -c` besides the probe.
BUNDLE_MARK = re.compile(r'cat (\S+) 2>/dev/null \|\| true\Z')
DIGEST_DIR = re.compile(r'[0-9a-f]{64}\Z')
# What a feed script's argv may contain: paths under the engine root, or plain
# tokens (digests, input ids, names). Anything else is refused.
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:+-]*\Z')


def inside(path, root):
    """True when `path` resolves within `root` itself or below it."""
    real = os.path.realpath(path)
    base = os.path.realpath(root)
    return real == base or real.startswith(base + os.sep)


def feed_hashes(engine_root):
    """Every allowlisted feed-script digest: provision's file plus each bundle's."""
    hashes = set()
    bundles = os.path.join(engine_root, 'bundles')
    paths = [os.path.join(engine_root, 'feeds.allow')]
    try:
        names = os.listdir(bundles)
    except OSError:
        names = []
    for name in names:
        paths.append(os.path.join(bundles, name, 'pandora', '.feeds'))
    for path in paths:
        try:
            with open(path) as handle:
                hashes.update(line.strip() for line in handle if line.strip())
        except OSError:
            continue
    return hashes


def check_feed(argv, engine_root):
    """`python3 -c <script> [args]`: allowlisted script, confined paths."""
    digest = hashlib.sha256(argv[2].encode()).hexdigest()
    if digest not in feed_hashes(engine_root):
        return 'python3 -c script %s is not on the feed allowlist' % digest[:12]
    for arg in argv[3:]:
        if arg.startswith('/'):
            if not inside(arg, engine_root):
                return 'feed argument %s is outside the engine root' % arg
        elif not TOKEN.fullmatch(arg):
            return 'feed argument %r is neither a token nor a confined path' % arg
    return None


def check_rsync(argv, engine_root):
    """`rsync --server ...`: every path operand must stay in the engine root."""
    if '--server' not in argv[1:]:
        return 'rsync without --server is a local-side command'
    for arg in argv[1:]:
        if arg.startswith('--link-dest='):
            if not inside(arg[len('--link-dest='):], engine_root):
                return 'link-dest %s is outside the engine root' % arg
        elif arg.startswith('-') or arg in ('.', ''):
            continue
        elif arg.startswith('/'):
            if not inside(arg, engine_root):
                return 'rsync path %s is outside the engine root' % arg
        else:
            return 'rsync argument %r is not a flag or a confined path' % arg
    return None


def check_sh(argv, engine_root):
    """`sh -c <script>`: the two fixed probes a client sends, nothing else."""
    if len(argv) != 3:
        return 'sh with arguments other than -c <script> is not admitted'
    script = argv[2]
    if script == HOME_PROBE:
        return None
    mark = BUNDLE_MARK.fullmatch(script)
    if mark and inside(mark.group(1), os.path.join(engine_root, 'bundles')) \
            and mark.group(1).endswith('/pandora/.bundle'):
        return None
    return 'sh -c %r is not an admitted probe' % script[:80]


def check_module(argv, engine_root, worker_root):
    """`cd <bundle> && PYTHONPATH=<bundle> python3 -m <module> --root ... <verb>`.

    `argv` is the whole command as one token list, `&&` surviving as a bare
    token only where it really joins two commands -- inside quotes shlex keeps
    it inside its word.
    """
    if argv.count('&&') != 1:
        return 'only the `cd <bundle> && python3 -m <module>` shape is admitted'
    joint = argv.index('&&')
    cd, tail = argv[:joint], argv[joint + 1:]
    if len(cd) != 2 or cd[0] != 'cd':
        return 'the first half of the command is not `cd <dir>`'
    bundle = cd[1]
    bundles = os.path.join(engine_root, 'bundles')
    if not inside(bundle, bundles) \
            or not DIGEST_DIR.fullmatch(os.path.basename(os.path.normpath(bundle))):
        return 'the working directory %s is not an installed bundle' % bundle
    if len(tail) < 4 or not tail[0].startswith('PYTHONPATH=') \
            or tail[1] != 'python3' or tail[2] != '-m':
        return 'the second half is not `PYTHONPATH=<bundle> python3 -m <module>`'
    if os.path.normpath(tail[0][len('PYTHONPATH='):]) != os.path.normpath(bundle):
        return 'PYTHONPATH does not match the cd target'
    module = tail[3]
    if module not in MODULES:
        return 'module %s is not admitted for this key' % module
    # The module's own flags lead: --root, --engine-root for the worker half.
    # `--python` is refused outright: it would name the interpreter a spawn runs.
    rest = tail[4:]
    roots = {}
    while rest and rest[0].startswith('--'):
        if rest[0] == '--python':
            return '--python is not admitted for this key'
        if len(rest) < 2:
            return 'flag %s has no value' % rest[0]
        roots[rest[0]] = rest[1]
        rest = rest[2:]
    if not rest:
        return 'no verb followed the flags'
    verb = rest[0]
    if verb not in MODULES[module]:
        return 'verb %s is not admitted for this key' % verb
    if any(token in ('|', '||', ';', '>', '<') for token in rest[1:]):
        return 'verb arguments may not contain shell operators'
    if module == 'pandora.engine.service':
        if os.path.normpath(roots.get('--root', '')) != os.path.normpath(engine_root):
            return '--root must be the engine root'
    else:
        if os.path.normpath(roots.get('--engine-root', '')) \
                != os.path.normpath(engine_root) \
                or os.path.normpath(roots.get('--root', '')) \
                != os.path.normpath(worker_root):
            return 'worker verbs must name the provisioned roots'
    return None


def check(command, *, engine_root, worker_root):
    """The verdict on one SSH_ORIGINAL_COMMAND: None admits, a string refuses."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return 'the command does not parse'
    if not argv:
        return 'empty command'
    if '&&' in argv:
        return check_module(argv, engine_root, worker_root)
    if argv[:2] == ['sh', '-c']:
        return check_sh(argv, engine_root)
    if argv[:2] == ['python3', '-c']:
        return check_feed(argv, engine_root)
    if argv[0] == 'rsync':
        return check_rsync(argv, engine_root)
    return 'command %r is not a shape this worker admits' % argv[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--name', required=True,
                        help="the client name this key speaks as")
    parser.add_argument('--engine-root', required=True)
    parser.add_argument('--worker-root', required=True)
    args = parser.parse_args(argv)
    command = os.environ.get('SSH_ORIGINAL_COMMAND', '')
    reason = check(command, engine_root=args.engine_root, worker_root=args.worker_root)
    if reason:
        sys.stderr.write('pandora-gateway: refused as %s: %s\n' % (args.name, reason))
        return 1
    # The pin, not the request, is the identity the engine records.
    os.environ['PANDORA_GATEWAY_CLIENT'] = args.name
    os.execvpe('sh', ['sh', '-c', command], os.environ)
    return 127


if __name__ == '__main__':
    raise SystemExit(main())
