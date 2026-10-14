"""Pure reuse decisions over authenticated evidence and separately trusted policy.

Authentication is the caller's responsibility. No execution skips are wired here.
"""
import hashlib
import json
import math
import re
import subprocess

FAMILIES = {'unit', 'surface', 'journey', 'arc', 'node', 'native', 'static'}
OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _path(value):
    if (not isinstance(value, str) or not value or value.startswith('/') or
            any(part in ('', '.', '..') for part in value.split('/')) or
            '\\' in value or '\x00' in value):
        raise ValueError('unsafe_input_path')
    return value


def input_manifest(worktree, tree, spec):
    """Read exact Git tree objects; never follow symlinks or inspect checkout files.

    Root closure covers every tracked entry. Scoped closure records absence,
    file modes and recursive directory membership. Gitlinks fail closed because
    their contents are not available in this repository's tree.
    """
    if not isinstance(tree, str) or not OID.fullmatch(tree):
        raise ValueError('invalid_tree')
    if not isinstance(spec, dict) or set(spec) != {'root', 'paths', 'directories'}:
        raise ValueError('invalid_input_spec')
    if type(spec['root']) is not bool:
        raise ValueError('invalid_input_spec')
    for key in ('paths', 'directories'):
        if not isinstance(spec[key], list) or len(set(spec[key])) != len(spec[key]):
            raise ValueError('invalid_input_spec')
        for path in spec[key]:
            _path(path)
    proc = subprocess.run(['git', '-C', str(worktree), 'ls-tree', '-r', '-z', tree],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                          check=True)
    entries = {}
    for raw in proc.stdout.split(b'\0'):
        if not raw:
            continue
        metadata, name = raw.split(b'\t', 1)
        mode, kind, oid = metadata.decode('ascii').split()
        name = name.decode('utf-8', 'strict')
        entries[name] = [mode, kind, oid]
    selected = entries if spec['root'] else {}
    selected = dict(selected)
    for path in spec['paths']:
        selected[path] = entries.get(path)
    directories = {}
    for path in spec['directories']:
        members = {name: value for name, value in entries.items() if name.startswith(path + '/')}
        # An empty Git directory does not exist; distinguish a file at this path.
        directories[path] = {'entry': entries.get(path), 'members': members}
        selected.update(members)
    if any(value and value[1] == 'commit' for value in selected.values()):
        raise ValueError('unsupported_gitlink')
    result = {'spec': spec, 'entries': selected, 'directories': directories}
    return hashlib.sha256(canonical(result)).hexdigest()


def _units(rows, *, evidence=False):
    if not isinstance(rows, list) or not rows:
        raise ValueError('invalid_units')
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
            raise ValueError('invalid_unit_identity')
        identity = row['id']
        if identity in result:
            raise ValueError('duplicate_unit_identity')
        if evidence:
            attempts = row.get('attempts')
            # A retry is diagnostic evidence, never reusable passing evidence.
            passed = (row.get('complete') is True and row.get('outcome') == 'passed' and
                      isinstance(attempts, list) and len(attempts) == 1 and
                      canonical(attempts[0]) == canonical({'ordinal': 0, 'outcome': 'passed'}))
            result[identity] = passed
        else:
            result[identity] = True
    return result


def evaluate(evidence, request, policy, *, manifest, now, force_full=False, canary=False):
    """Return eligibility separately from skip candidates; malformed input fails closed.

    `manifest(tree, spec)` must read immutable Git trees. `evidence` is the
    authenticated, digest-bound report, not arbitrary unverified client input.
    Policy comes from trusted main; never from the candidate branch.
    """
    result = {'reusable': False, 'whole_complete': False, 'eligible_units': [],
              'remaining_units': [], 'skip_units': [], 'reason': 'reuse_unavailable',
              'diagnostics': []}
    try:
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError('invalid_time')
        for value in (evidence, request, policy):
            if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1:
                raise ValueError('unsupported_schema')
        for value in (evidence, request):
            tree = value.get('source_tree')
            if not isinstance(tree, str) or not OID.fullmatch(tree):
                raise ValueError('invalid_tree')
        wanted = _units(request.get('units'))
        result['remaining_units'] = sorted(wanted)
        observed = _units(evidence.get('units'), evidence=True)
        for key in ('repo', 'check_id', 'family'):
            value = request.get(key)
            if not isinstance(value, str) or not value or evidence.get(key) != value or policy.get(key) != value:
                raise ValueError('context_mismatch:' + key)
        if request['family'] not in FAMILIES:
            raise ValueError('unsupported_family')
        selection = request.get('selection')
        if not isinstance(selection, dict) or canonical(evidence.get('selection')) != canonical(selection):
            raise ValueError('selection_mismatch')
        runtime = request.get('runtime')
        if (not isinstance(runtime, dict) or not runtime or
                canonical(evidence.get('runtime')) != canonical(runtime) or
                canonical(policy.get('runtime')) != canonical(runtime)):
            raise ValueError('runtime_mismatch')
        # Canonical bytes also reject values unsupported by the protocol.
        canonical(runtime)
        engine = evidence.get('engine_identity')
        approved = policy.get('approved_engines')
        if (not isinstance(approved, list) or not approved or
                any(not isinstance(item, str) or not re.fullmatch(r'[0-9a-f]{64}', item)
                    for item in approved)):
            raise ValueError('invalid_engine_policy')
        if (not isinstance(engine, str) or not re.fullmatch(r'[0-9a-f]{64}', engine) or
                engine != request.get('engine_identity') or engine not in approved):
            raise ValueError('engine_mismatch')
        age = policy.get('max_age_seconds')
        finished = evidence.get('finished')
        if (type(age) is not int or age <= 0 or type(finished) not in (int, float) or
                not math.isfinite(finished)):
            raise ValueError('invalid_freshness')
        if not 0 <= now - finished <= age:
            raise ValueError('stale_or_future_evidence')
        spec = policy.get('inputs')
        if not isinstance(spec, dict):
            raise ValueError('invalid_input_spec')
        required = policy.get('required_paths')
        if not isinstance(required, list) or not required:
            raise ValueError('missing_producer_config_inputs')
        for path in required:
            _path(path)
            if not (spec.get('root') is True or path in spec.get('paths', []) or
                    any(path.startswith(directory + '/') for directory in spec.get('directories', []))):
                raise ValueError('missing_producer_config_inputs')
        if canonical(evidence.get('inputs')) != canonical(spec):
            raise ValueError('input_spec_mismatch')
        previous = manifest(evidence.get('source_tree'), spec)
        current = manifest(request.get('source_tree'), spec)
        if any(not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
               for digest in (previous, current)):
            raise ValueError('invalid_manifest_digest')
        if previous != evidence.get('input_manifest'):
            raise ValueError('evidence_manifest_mismatch')
        if previous != current:
            raise ValueError('inputs_changed')
        if evidence.get('complete') is not True or evidence.get('outcome') != 'passed':
            raise ValueError('evidence_not_complete_pass')
        granularity = policy.get('granularity', 'invocation')
        if granularity not in ('invocation', 'independent-unit'):
            raise ValueError('unsupported_granularity')
        eligible = sorted(identity for identity in wanted if observed.get(identity))
        if granularity == 'invocation' and (set(wanted) != set(observed) or len(eligible) != len(wanted)):
            eligible = []
        result.update(eligible_units=eligible,
                      remaining_units=sorted(set(wanted) - set(eligible)),
                      whole_complete=len(eligible) == len(wanted))
        if canary or force_full:
            result['reason'] = 'canary' if canary else 'force_full'
            result['diagnostics'].append('eligible evidence retained; execution required')
        else:
            result.update(skip_units=eligible, reusable=bool(eligible),
                          reason='match' if eligible else 'no_complete_matching_units')
    except (ValueError, TypeError, KeyError, AttributeError, OSError,
            subprocess.SubprocessError, OverflowError, RecursionError) as error:
        result['reason'] = str(error) if isinstance(error, ValueError) else 'reuse_unavailable'
        result['skip_units'] = []
        result['reusable'] = False
    return result
