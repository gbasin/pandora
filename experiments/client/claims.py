"""Authoritative classification, and the compact claim index the shim reads.

Two tiers on purpose.

The daemon holds the *real* classifier -- the config-driven one copied verbatim
from ``poc/ci-import`` (``repo_config/classify.py``, ``config.py``,
``ci_import.py``, plus the eichler example config and the ci.yml fixture).
Loading that configuration costs ~50 ms on this Mac, which is five times the
entire latency budget for a shim invocation, so it can never live in the shim.

The shim holds a *derived* index: the flat list of claimed argv prefixes, written
into the enrolment marker.  The shim's job is only to answer "could this be
claimed?" in microseconds.  The daemon re-classifies every request it receives
and may still refuse; the index is an optimisation, never the decision.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Commands that are unclaimed but expensive enough that the owner wants to know
# they still ran locally.  This is what feeds the passthrough log.
STUB_CLAIMS = [['test:unit'], ['journeys']]
STUB_HEAVY = [['build'], ['lint'], ['typecheck'], ['test:e2e'], ['install']]
DEFAULT_HEAVY = [['build'], ['lint'], ['typecheck'], ['install'], ['dev'],
                 ['test:ios'], ['test:races'], ['exec']]


def load_config(toml_path, root=None):
    """The config-driven classifier, or None when it cannot be loaded cheaply."""
    sys.path.insert(0, str(HERE / 'repo_config'))
    try:
        import config as repo_config
        return repo_config.load(toml_path, root=root)
    except Exception:                   # noqa: BLE001 -- any failure means "use the stub"
        return None


def index_from_config(config):
    """Every claimed argv prefix the configuration declares, shortest first."""
    claims = []
    for job in config['jobs'].values():
        for form in job['forms']:
            prefix = list(form['prefix'])
            if prefix not in claims:
                claims.append(prefix)
    claims.sort(key=lambda item: (len(item), item))
    return claims


def stub_index():
    return list(STUB_CLAIMS)


def decide(daemon, request):
    """Daemon-side verdict for one request.

    ``remote`` means the daemon will run it.  Anything else is returned to the
    client before acceptance, so the client may still run it locally.
    """
    argv = list(request.get('argv') or [])
    if not argv:
        return {'decision': 'reject', 'code': 'rejected', 'message': 'empty argv'}
    repo = daemon.config.get('repos') or []
    if repo and request.get('cwd'):
        cwd = str(request['cwd'])
        if not any(cwd == root or cwd.startswith(root.rstrip('/') + '/') for root in repo):
            return {'decision': 'reject', 'code': 'unenrolled',
                    'message': 'cwd is not inside an enrolled repository'}
    config = getattr(daemon, 'repo_config', None)
    if config is not None:
        try:
            sys.path.insert(0, str(HERE / 'repo_config'))
            import classify
            verdict = classify.classify(config, argv, cwd='.',
                                        env=request.get('env') or {})
        except Exception as error:      # noqa: BLE001
            return {'decision': 'reject', 'code': 'rejected', 'message': str(error)}
        if verdict['decision'] == 'remote':
            return {'decision': 'remote'}
        if verdict['decision'] == 'local':
            return {'decision': 'reject', 'code': 'rejected',
                    'message': verdict.get('reason', 'not claimed')}
        return {'decision': 'reject', 'code': 'rejected', 'message': verdict['message']}
    tail = argv[1:] if argv[:1] == ['pnpm'] else argv
    if tail[:1] == ['run']:
        tail = tail[1:]
    for claim in daemon.config.get('claims', STUB_CLAIMS):
        if tail[:len(claim)] == list(claim):
            return {'decision': 'remote'}
    return {'decision': 'reject', 'code': 'rejected', 'message': 'no configured job claims this command'}
