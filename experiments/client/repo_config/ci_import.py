"""Import facts from a GitHub Actions workflow job, by reference.

A ``pandora.toml`` job may name ``ci_job = "journeys"``; the facts that the
repository already maintains in its workflow -- service containers, job-level
``env``, the matrix shard dimension, ``timeout-minutes``, artifact paths and the
Node version -- are then read from the workflow instead of restated.

The reader is deliberately unforgiving.  Anything it cannot interpret inside a
field it would inherit is a named ``CiImportError`` that quotes the workflow
path, the job, the field and the offending text.  It never skips silently: an
import that succeeds means every byte of every inherited field was understood.

Python's standard library has no YAML parser, so :func:`load_workflow` tries, in
order, an importable PyYAML, then ``ruby -ryaml -rjson``, then a committed JSON
snapshot of the workflow.  See ``notes/repo-config-contract-draft.md`` for which
one this POC recommends shipping.
"""
import json
import re
import shlex
import subprocess
from pathlib import Path

VERSION = 1

# Job keys the importer reads.
JOB_READ = ('services', 'env', 'strategy', 'timeout-minutes', 'steps')
# Job keys that exist for GitHub's scheduler and mean nothing to a worker that
# runs one command on behalf of one agent.  Ignoring these is a decision, not an
# oversight, so they are listed rather than defaulted.
JOB_IGNORED = ('name', 'needs', 'if', 'runs-on', 'permissions', 'outputs',
               'concurrency', 'continue-on-error', 'defaults', 'environment')
# Job keys that make the job un-importable, with the reason printed to the user.
JOB_REFUSED = {
    'container': 'the job body runs inside its own container image; [runtime] in pandora.toml '
                 'already owns that choice and the two cannot be reconciled by inheritance',
    'uses': 'the job is a reusable-workflow call, so there is no job body in this file to import',
    'secrets': 'pandora never forwards repository secrets into an agent run',
    'strategy.matrix.exclude': 'an excluded matrix combination has no shard-count meaning',
}

SERVICE_READ = ('image', 'env', 'ports', 'options')
SERVICE_REFUSED = {
    'credentials': 'a registry login is operator state, not repository state',
    'volumes': 'a host bind mount cannot exist on a worker that runs from a frozen snapshot',
}

ARTIFACT_WITH = ('name', 'path', 'retention-days', 'if-no-files-found',
                 'include-hidden-files', 'overwrite', 'compression-level')

EXPRESSION = re.compile(r'\$\{\{(.*?)\}\}')
MATRIX_REF = re.compile(r'\s*matrix\.([A-Za-z_][A-Za-z0-9_-]*)\s*\Z')
SHARD_VALUE = re.compile(r'([0-9]+)/([0-9]+)\Z')
DURATION = re.compile(r'([0-9]+)(ms|s|m)\Z')
USES = re.compile(r'([^@]+)@(.+)\Z')
HEALTH_FLAGS = ('--health-cmd', '--health-interval', '--health-timeout',
                '--health-retries', '--health-start-period')


class CiImportError(ValueError):
    """A workflow fact Pandora refuses to guess at."""


# ---------------------------------------------------------------------------
# YAML, without a YAML parser in the standard library
# ---------------------------------------------------------------------------

def _pyyaml(text, where):
    """Parse with PyYAML if it is importable, rejecting anchors and aliases."""
    try:
        import yaml
    except ImportError:
        return None

    class Strict(yaml.SafeLoader):
        def compose_node(self, parent, index):
            event = self.peek_event()
            if isinstance(event, yaml.events.AliasEvent):
                raise CiImportError('%s uses the YAML alias *%s; pandora reads workflows literally '
                                    'and refuses anchored documents' % (where, event.anchor))
            if getattr(event, 'anchor', None):
                raise CiImportError('%s defines the YAML anchor &%s; pandora reads workflows '
                                    'literally and refuses anchored documents'
                                    % (where, event.anchor))
            return super().compose_node(parent, index)

        def construct_mapping(self, node, deep=False):
            seen = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=True)
                if key in seen:
                    raise CiImportError('%s repeats the key %r' % (where, key))
                seen.add(key)
            return super().construct_mapping(node, deep)

    return yaml.load(text, Loader=Strict)


RUBY = ('ruby', '-ryaml', '-rjson', '-e',
        'doc = YAML.safe_load(STDIN.read, aliases: false); STDOUT.write(JSON.generate(doc))')


def _ruby(text, where):
    """Parse by shelling out to Ruby's Psych, which macOS ships."""
    try:
        done = subprocess.run(RUBY, input=text, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        detail = done.stderr.strip().splitlines()[-1] if done.stderr.strip() else 'ruby failed'
        raise CiImportError('%s could not be parsed by ruby: %s' % (where, detail))
    return json.loads(done.stdout)


def load_workflow(path):
    """Return ``(document, parser)`` for a workflow file or a JSON snapshot of one.

    ``parser`` is one of ``pyyaml``, ``ruby`` or ``snapshot``.  A ``.json`` path
    is read as a committed snapshot: ``{"version", "source", "sha256", "workflow"}``.
    """
    path = Path(path)
    try:
        text = path.read_text()
    except OSError as error:
        raise CiImportError('cannot read workflow %s: %s' % (path, error)) from None
    where = path.name
    if path.suffix == '.json':
        return _snapshot(text, path), 'snapshot'
    parser = 'pyyaml'
    document = _pyyaml(text, where)
    if document is None:
        parser, document = 'ruby', _ruby(text, where)
    if document is None:
        raise CiImportError(
            'cannot parse %s: the standard library has no YAML parser, PyYAML is not importable '
            'and ruby is not on PATH.  Commit a JSON snapshot of the workflow and point '
            'ci_workflow at it instead.' % path)
    if not isinstance(document, dict) or not isinstance(document.get('jobs'), dict):
        raise CiImportError('%s has no jobs: table' % path)
    return document, parser


def _snapshot(text, path):
    try:
        value = json.loads(text)
    except ValueError as error:
        raise CiImportError('%s is not valid JSON: %s' % (path, error)) from None
    keys = {'version', 'source', 'sha256', 'workflow'}
    if not isinstance(value, dict) or set(value) != keys:
        raise CiImportError('%s must be a snapshot with keys %s' % (path, ', '.join(sorted(keys))))
    if value['version'] != VERSION:
        raise CiImportError('%s snapshot version must be %d' % (path, VERSION))
    return value['workflow']


def snapshot_is_fresh(snapshot_path, workflow_path):
    """True when a committed snapshot still matches the workflow it was made from."""
    import hashlib
    value = json.loads(Path(snapshot_path).read_text())
    digest = hashlib.sha256(Path(workflow_path).read_bytes()).hexdigest()
    return value.get('sha256') == digest


# ---------------------------------------------------------------------------
# Scalars, expressions, durations
# ---------------------------------------------------------------------------

def _expressions(text, where):
    """Return the matrix dimensions a string references; refuse anything else."""
    names = []
    for match in EXPRESSION.finditer(text):
        inner = MATRIX_REF.fullmatch(match.group(1))
        if inner is None:
            raise CiImportError('%s uses the expression ${{%s}}; only ${{ matrix.<name> }} can be '
                                'imported, because everything else is evaluated by GitHub'
                                % (where, match.group(1)))
        names.append(inner.group(1))
    return names


def _scalar(value, where):
    """Normalize a YAML scalar the way GitHub stringifies an env value."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int) or isinstance(value, float):
        return repr(value) if isinstance(value, float) else str(value)
    if value is None:
        raise CiImportError('%s is empty; write an explicit "" if that is meant' % where)
    if not isinstance(value, str):
        raise CiImportError('%s must be a scalar, not %s' % (where, type(value).__name__))
    return value


def _env_table(value, where):
    if not isinstance(value, dict):
        raise CiImportError(where + ' must be a mapping')
    table = {}
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', str(key)):
            raise CiImportError('%s has an invalid variable name: %r' % (where, key))
        text = _scalar(item, '%s.%s' % (where, key))
        _expressions(text, '%s.%s' % (where, key))
        table[key] = text
    return table


def _millis(text, where):
    match = DURATION.fullmatch(str(text).strip())
    if match is None:
        raise CiImportError('%s is not a duration like 5s, 500ms or 1m: %s' % (where, text))
    scale = {'ms': 1, 's': 1000, 'm': 60000}[match.group(2)]
    return int(match.group(1)) * scale


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

def _options(text, where):
    """Parse a service's ``options:`` string into a healthcheck."""
    try:
        tokens = shlex.split(text)
    except ValueError as error:
        raise CiImportError('%s is not a shell-quotable option string: %s' % (where, error)) from None
    health, index = {}, 0
    while index < len(tokens):
        token = tokens[index]
        if token not in HEALTH_FLAGS:
            raise CiImportError('%s uses the docker option %s; pandora imports only %s, because '
                                'every other option changes how the container is isolated'
                                % (where, token, ', '.join(HEALTH_FLAGS)))
        if index + 1 >= len(tokens):
            raise CiImportError('%s: %s has no value' % (where, token))
        health[token] = tokens[index + 1]
        index += 2
    if not health:
        return None
    if '--health-cmd' not in health:
        raise CiImportError(where + ' sets health options without --health-cmd')
    try:
        argv = shlex.split(health['--health-cmd'])
    except ValueError as error:
        raise CiImportError('%s: --health-cmd is not quotable: %s' % (where, error)) from None
    if not argv:
        raise CiImportError(where + ': --health-cmd is empty')
    return {
        'argv': argv,
        'attempts': int(health.get('--health-retries', 10)),
        'interval_ms': _millis(health.get('--health-interval', '5s'), where + ' --health-interval'),
        'timeout_ms': _millis(health.get('--health-timeout', '5s'), where + ' --health-timeout'),
        'start_period_ms': _millis(health['--health-start-period'], where + ' --health-start-period')
                           if '--health-start-period' in health else 0,
    }


def _ports(value, where):
    if not isinstance(value, list):
        raise CiImportError(where + ' must be a list')
    ports = []
    for index, item in enumerate(value):
        text = _scalar(item, '%s[%d]' % (where, index))
        host, _, container = text.partition(':')
        if not container:
            host, container = '', host
        if not container.isdigit() or (host and not host.isdigit()):
            raise CiImportError('%s[%d] is not HOST:CONTAINER or CONTAINER: %s'
                                % (where, index, text))
        ports.append({'host': int(host) if host else int(container), 'container': int(container)})
    return ports


def _service(name, value, where):
    if not isinstance(value, dict):
        raise CiImportError(where + ' must be a mapping')
    for key in sorted(value):
        if key in SERVICE_REFUSED:
            raise CiImportError('%s.%s cannot be imported: %s' % (where, key, SERVICE_REFUSED[key]))
        if key not in SERVICE_READ:
            raise CiImportError('%s has the unknown key %s; pandora imports %s'
                                % (where, key, ', '.join(SERVICE_READ)))
    if 'image' not in value:
        raise CiImportError(where + ' has no image')
    image = _scalar(value['image'], where + '.image')
    _expressions(image, where + '.image')
    return {
        'name': name,
        'image': image,
        'env': _env_table(value.get('env', {}), where + '.env'),
        'ports': _ports(value.get('ports', []), where + '.ports'),
        'health': _options(_scalar(value['options'], where + '.options'), where + '.options')
                  if 'options' in value else None,
    }


# ---------------------------------------------------------------------------
# Matrix and its consumption
# ---------------------------------------------------------------------------

def _shard_dimension(values, where):
    """Return the shard total when every value is ``i/n`` over a complete 1..n."""
    seen = {}
    total = None
    for item in values:
        match = SHARD_VALUE.fullmatch(str(item).strip())
        if match is None:
            return None
        index, count = int(match.group(1)), int(match.group(2))
        if total is None:
            total = count
        elif total != count:
            raise CiImportError('%s mixes shard totals %d and %d' % (where, total, count))
        seen[index] = seen.get(index, 0) + 1
    if total is None or sorted(seen) != list(range(1, total + 1)):
        raise CiImportError('%s is not a complete 1..n shard list: %s' % (where, values))
    return total


def _matrix(strategy, where, params):
    """Return ``(dimension, total)`` for the shard axis, or ``None``."""
    if not isinstance(strategy, dict):
        raise CiImportError(where + ' must be a mapping')
    for key in strategy:
        if key not in ('matrix', 'fail-fast', 'max-parallel'):
            raise CiImportError('%s has the unknown key %s' % (where, key))
    matrix = strategy.get('matrix')
    if matrix is None:
        return None
    if not isinstance(matrix, dict):
        raise CiImportError(where + '.matrix must be a mapping')
    if 'exclude' in matrix:
        raise CiImportError('%s.matrix.exclude cannot be imported: %s'
                            % (where, JOB_REFUSED['strategy.matrix.exclude']))
    columns = {}
    if 'include' in matrix:
        if set(matrix) != {'include'}:
            raise CiImportError(where + '.matrix mixes include with plain dimensions, whose '
                                        'cross product pandora will not reconstruct')
        rows = matrix['include']
        if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
            raise CiImportError(where + '.matrix.include must be a nonempty list of mappings')
        keys = set(rows[0])
        for row in rows:
            if set(row) != keys:
                raise CiImportError(where + '.matrix.include rows do not all set the same keys')
        for key in keys:
            columns[key] = [row[key] for row in rows]
    else:
        for key, item in matrix.items():
            if not isinstance(item, list):
                raise CiImportError('%s.matrix.%s must be a list' % (where, key))
            columns[key] = item
    shard, total = None, None
    for key in sorted(columns):
        found = _shard_dimension(columns[key], '%s.matrix.%s' % (where, key))
        if found is None:
            if key not in params:
                raise CiImportError(
                    '%s.matrix.%s is a dimension pandora cannot inherit: its values %s are not '
                    'shards.  An agent chooses it, so declare it with ci_matrix_params = ["%s"] '
                    'and let the argv carry it.' % (where, key, columns[key], key))
            continue
        if shard is not None:
            raise CiImportError('%s.matrix has two shard dimensions, %s and %s' % (where, shard, key))
        shard, total = key, found
    if shard is None:
        return None
    return {'dimension': shard, 'total': total}


def _consumption(steps, dimension, where):
    """Find the single place a shard value reaches the command."""
    assign = re.compile(r'(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=([\'"]?)\$\{\{\s*matrix\.%s\s*\}\}\2'
                        % re.escape(dimension))
    option = re.compile(r'(--[A-Za-z0-9][A-Za-z0-9-]*)=([\'"]?)\$\{\{\s*matrix\.%s\s*\}\}\2'
                        % re.escape(dimension))
    found = []
    for index, step in enumerate(steps):
        spot = '%s.steps[%d]' % (where, index)
        for key, item in (step.get('env') or {}).items():
            if dimension in _expressions(_scalar(item, spot + '.env.' + key), spot + '.env.' + key):
                found.append({'kind': 'env', 'name': key, 'where': spot + '.env.' + key})
        run = step.get('run')
        if not isinstance(run, str):
            continue
        for match in option.finditer(run):
            found.append({'kind': 'argv', 'name': match.group(1), 'where': spot + '.run'})
        for match in assign.finditer(run):
            if not any(f['kind'] == 'argv' and f['where'] == spot + '.run' for f in found):
                found.append({'kind': 'env', 'name': match.group(1), 'where': spot + '.run'})
    unique = {(f['kind'], f['name']): f for f in found}
    if not unique:
        raise CiImportError(
            '%s never consumes matrix.%s in a form pandora recognizes.  A shard reaches the '
            'command either as NAME=${{ matrix.%s }} or as --flag=${{ matrix.%s }}; if it is '
            'passed through a composite action or a script argument, the workflow cannot be the '
            'source of that fact.' % (where, dimension, dimension, dimension))
    if len(unique) > 1:
        raise CiImportError('%s consumes matrix.%s in %d different ways: %s'
                            % (where, dimension, len(unique),
                               ', '.join('%s %s' % k for k in sorted(unique))))
    return next(iter(unique.values()))


# ---------------------------------------------------------------------------
# Steps: artifacts and toolchain
# ---------------------------------------------------------------------------

def _action(step):
    uses = step.get('uses')
    if not isinstance(uses, str):
        return None, None
    match = USES.fullmatch(uses)
    return (match.group(1), match.group(2)) if match else (uses, None)


def _artifact(step, spot):
    with_ = step.get('with')
    if not isinstance(with_, dict):
        raise CiImportError(spot + ' uploads an artifact with no with: block')
    for key in with_:
        if key not in ARTIFACT_WITH:
            raise CiImportError('%s.with has the unknown key %s; pandora imports path and ignores %s'
                                % (spot, key, ', '.join(k for k in ARTIFACT_WITH if k != 'path')))
    if 'path' not in with_:
        raise CiImportError(spot + '.with has no path')
    paths, names = [], []
    for line in _scalar(with_['path'], spot + '.with.path').splitlines():
        text = line.strip()
        if not text:
            continue
        names.extend(_expressions(text, spot + '.with.path'))
        paths.append(text)
    if not paths:
        raise CiImportError(spot + '.with.path is empty')
    return {'paths': paths, 'dimensions': sorted(set(names)), 'condition': step.get('if'),
            'where': spot + '.with.path'}


def _steps(steps, where):
    if not isinstance(steps, list):
        raise CiImportError(where + '.steps must be a list')
    artifacts, node, clean = [], None, []
    for index, step in enumerate(steps):
        spot = '%s.steps[%d]' % (where, index)
        if not isinstance(step, dict):
            raise CiImportError(spot + ' must be a mapping')
        action, _version = _action(step)
        if action is None and not isinstance(step.get('run'), str):
            raise CiImportError(spot + ' has neither run: nor uses:')
        if action == 'actions/upload-artifact':
            artifacts.append(_artifact(step, spot))
        elif action == 'actions/setup-node':
            with_ = step.get('with') or {}
            if 'node-version-file' in with_:
                raise CiImportError(spot + '.with.node-version-file points outside the workflow; '
                                           'pandora imports only an inline node-version')
            if 'node-version' in with_:
                node = {'version': _scalar(with_['node-version'], spot + '.with.node-version'),
                        'where': spot + '.with.node-version'}
        clean.append(step)
    return artifacts, node, clean


# ---------------------------------------------------------------------------
# The import itself
# ---------------------------------------------------------------------------

def import_job(document, job_name, *, source='ci.yml', matrix_params=()):
    """Return the facts ``job_name`` can lend a pandora.toml job, with provenance."""
    jobs = document.get('jobs') if isinstance(document, dict) else None
    if not isinstance(jobs, dict):
        raise CiImportError('%s has no jobs: table' % source)
    if job_name not in jobs:
        raise CiImportError('%s has no job %r; it defines %s'
                            % (source, job_name, ', '.join(sorted(jobs))))
    job = jobs[job_name]
    where = '%s:%s' % (source, job_name)
    if not isinstance(job, dict):
        raise CiImportError(where + ' must be a mapping')
    for key in sorted(job):
        if key in JOB_REFUSED:
            raise CiImportError('%s.%s cannot be imported: %s' % (where, key, JOB_REFUSED[key]))
        if key not in JOB_READ and key not in JOB_IGNORED:
            raise CiImportError('%s has the unknown key %s; pandora reads %s and deliberately '
                                'ignores %s' % (where, key, ', '.join(JOB_READ),
                                                ', '.join(JOB_IGNORED)))
    facts = {'version': VERSION, 'source': source, 'job': job_name,
             'services': {}, 'env': {}, 'shards': None, 'timeout_minutes': None,
             'artifacts': [], 'node': None, 'provenance': {}}

    def note(field, suffix):
        facts['provenance'][field] = '%s.%s' % (where, suffix)

    services = job.get('services') or {}
    if not isinstance(services, dict):
        raise CiImportError(where + '.services must be a mapping')
    for name in services:
        spot = '%s.services.%s' % (where, name)
        facts['services'][name] = _service(name, services[name], spot)
        note('services.' + name, 'services.' + name)
    facts['env'] = _env_table(job.get('env') or {}, where + '.env')
    for key in facts['env']:
        note('env.' + key, 'env.' + key)
    if 'timeout-minutes' in job:
        value = job['timeout-minutes']
        if type(value) is not int or value < 1:
            raise CiImportError(where + '.timeout-minutes must be a positive integer')
        facts['timeout_minutes'] = value
        note('timeout_minutes', 'timeout-minutes')
    artifacts, node, steps = _steps(job.get('steps') or [], where)
    facts['artifacts'] = artifacts
    if artifacts:
        note('artifacts', 'steps[*].uses=actions/upload-artifact')
    if node is not None:
        facts['node'] = node['version']
        note('node', 'steps[*].uses=actions/setup-node')
    if 'strategy' in job:
        shard = _matrix(job['strategy'], where + '.strategy', tuple(matrix_params))
        if shard is not None:
            shard['consumed'] = _consumption(steps, shard['dimension'], where)
            facts['shards'] = shard
            note('shards', 'strategy.matrix.' + shard['dimension'])
    return facts


# ---------------------------------------------------------------------------
# Normalized facts, for drift reporting
# ---------------------------------------------------------------------------

def clean_path(text):
    """Compare ``results/`` and ``results`` as the same directory."""
    return text.rstrip('/') or '/'


def normalize(facts, *, pins=None, roles=None):
    """Flatten imported facts into comparable ``field -> value`` pairs."""
    pins, roles = pins or {}, roles or {}
    flat = {}
    for name, service in facts['services'].items():
        role = roles.get(name, name)
        flat['services.%s.image' % role] = pins.get(service['image'], service['image'])
        flat['services.%s.env' % role] = dict(service['env'])
        flat['services.%s.ports' % role] = [p['container'] for p in service['ports']]
        flat['services.%s.health' % role] = (service['health'] or {}).get('argv')
    for key, value in facts['env'].items():
        flat['env.' + key] = value
    flat['shards.total'] = facts['shards']['total'] if facts['shards'] else None
    flat['shards.consumed'] = ('%s %s' % (facts['shards']['consumed']['kind'],
                                          facts['shards']['consumed']['name'])
                               if facts['shards'] else None)
    flat['timeout_minutes'] = facts['timeout_minutes']
    flat['node'] = facts['node']
    flat['artifacts'] = sorted(clean_path(p) for entry in facts['artifacts'] for p in entry['paths'])
    return flat


MISSING = object()


def drift(ci_flat, pandora_flat, exceptions=()):
    """Return findings for facts the two sides state differently, or at all."""
    import fnmatch
    findings = []
    for field in sorted(set(ci_flat) | set(pandora_flat)):
        if any(fnmatch.fnmatch(field, pattern) for pattern in exceptions):
            continue
        left, right = ci_flat.get(field, MISSING), pandora_flat.get(field, MISSING)
        if left is MISSING:
            kind = 'only-in-pandora'
        elif right is MISSING:
            kind = 'only-in-ci'
        elif left == right:
            continue
        else:
            kind = 'differs'
        findings.append({'field': field, 'kind': kind,
                         'ci': None if left is MISSING else left,
                         'pandora': None if right is MISSING else right})
    return findings
