#!/usr/bin/env python3
"""Authenticate immutable execution evidence and emit a fail-closed unit plan."""
import argparse
from collections import Counter
import hashlib
import fnmatch
import json
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pandora.client import execution_reuse as reuse, verdicts  # noqa: E402

MAX_BYTES = 4 * 1024 * 1024
MAX_RUNS = 128
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
CONTEXT = ('golden', 'golden_pins', 'env_digest', 'cwd')


def validate_command(argv, command, evidence, inventory, inputs):
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise ValueError('invalid_command')
    if isinstance(command, list):
        if argv != command:
            raise ValueError('command_mismatch')
        return
    if (not isinstance(command, dict) or not {'prefix', 'suffix'} <= set(command) or
            set(command) - {'prefix', 'suffix', 'unit_arguments', 'unit_path_prefix', 'literal_arguments', 'allowed_flags'} or
            ((command.get('unit_arguments') is True) == (command.get('literal_arguments') is True))):
        raise ValueError('invalid_command_policy')
    path_prefix = command.get('unit_path_prefix')
    if path_prefix is not None:
        reuse._path(path_prefix)
    prefix, suffix = command['prefix'], command['suffix']
    if (not isinstance(prefix, list) or not prefix or not isinstance(suffix, list) or
            not all(isinstance(arg, str) for arg in prefix + suffix) or
            len(argv) < len(prefix) + len(suffix) or argv[:len(prefix)] != prefix or
            (suffix and argv[-len(suffix):] != suffix)):
        raise ValueError('command_mismatch')
    arguments = argv[len(prefix):len(argv) - len(suffix) if suffix else len(argv)]
    if command.get('literal_arguments') is True:
        if path_prefix is not None:
            raise ValueError('invalid_command_policy')
        flags = command.get('allowed_flags', [])
        if (not isinstance(flags, list) or len(set(flags)) != len(flags) or
                not all(isinstance(flag, str) and re.fullmatch(r'--[A-Za-z][A-Za-z0-9-]*', flag) for flag in flags)):
            raise ValueError('invalid_command_policy')
        for argument in arguments:
            if argument in flags:
                continue
            if (not re.fullmatch(r'[A-Za-z0-9_./:@-]+', argument) or argument.startswith('-') or
                    any(part in ('', '.', '..') for part in argument.split('/'))):
                raise ValueError('unsafe_literal_selector')
        return
    units = evidence.get('units')
    if not isinstance(units, list):
        raise ValueError('invalid_command_units')
    names = {unit.get('command_arg') for unit in units if isinstance(unit, dict)}
    identities = {unit.get('id') for unit in units if isinstance(unit, dict)}
    if len(set(arguments)) != len(arguments):
        raise ValueError('duplicate_command_units')
    entries = inventory.get('entries') if isinstance(inventory, dict) else None
    if not isinstance(entries, dict) or not isinstance(inputs, dict):
        raise ValueError('missing_command_inputs')
    selected = set()
    for argument in arguments:
        reuse._path(argument)
        source = argument
        if source not in entries and path_prefix is not None:
            source = path_prefix + '/' + argument
        entry = entries.get(source)
        if (argument.startswith('-') or any(char in argument for char in '*?[]') or
                source not in names or source not in identities or not isinstance(entry, list) or len(entry) != 3 or
                entry[:2] != ['100644', 'blob'] or
                not (inputs.get('root') is True or source in inputs.get('paths', []) or
                     any(source.startswith(path + '/') for path in inputs.get('directories', [])))):
            raise ValueError('unsafe_command_unit')
        selected.add(source)
    if arguments and selected != names:
        raise ValueError('command_units_mismatch')


def authenticate(payload, raw, *, repo, tree, job, run_id, policy):
    body = json.loads(payload)
    if (body.get('kind') != 'pandora-verdict' or type(body.get('v')) is not int or
            body['v'] != 1 or body.get('outcome') != 'passed' or
            body.get('repo') != repo or body.get('tree') != tree or
            body.get('job') != job or body.get('run_id') != run_id):
        raise ValueError('envelope_context_mismatch')
    if body.get('source_integrity') is not True:
        raise ValueError('source_integrity_unproven')
    binding = body.get('test_evidence')
    if (not isinstance(binding, dict) or type(binding.get('bytes')) is not int or
            binding['bytes'] != len(raw) or binding.get('sha256') != hashlib.sha256(raw).hexdigest()):
        raise ValueError('report_digest_mismatch')
    context = policy.get('execution_context')
    if (not isinstance(context, dict) or set(context) != set(CONTEXT) or
            any(key not in body or reuse.canonical(context[key]) != reuse.canonical(body[key])
                for key in CONTEXT)):
        raise ValueError('execution_context_mismatch')
    report = json.loads(raw)
    if (report.get('kind') != 'pandora-execution-report' or type(report.get('schema')) is not int or
            report['schema'] != 1 or report.get('worker_run') != run_id):
        raise ValueError('unsupported_execution_report')
    evidence = report.get('execution')
    if not isinstance(evidence, dict):
        raise ValueError('missing_execution_evidence')
    evidence = dict(evidence)
    authoritative = {'repo': repo, 'source_tree': tree, 'engine_identity': body.get('engine'),
                     'finished': body.get('finished')}
    for key, value in authoritative.items():
        if key in evidence and reuse.canonical(evidence[key]) != reuse.canonical(value):
            raise ValueError('report_context_conflict')
    evidence.update(authoritative)
    evidence['inputs'] = policy.get('inputs')
    validate_command(body.get('argv'), policy.get('command'), evidence,
                     body.get('input_inventory'), evidence['inputs'])
    evidence['input_manifest'] = reuse.attested_manifest(body.get('input_inventory'), tree, evidence['inputs'])
    return evidence, body


def verify_adapters(body, policy, trusted_inventory, current_inventory):
    paths = policy.get('trusted_adapter_paths')
    if (not isinstance(paths, list) or not paths or len(set(paths)) != len(paths) or
            not isinstance(trusted_inventory, dict)):
        raise ValueError('missing_trusted_adapters')
    entries = body.get('input_inventory', {}).get('entries', {})
    inputs = policy.get('inputs', {})
    for path in paths:
        reuse._path(path)
        trusted = trusted_inventory.get(path)
        scoped = (inputs.get('root') is True or path in inputs.get('paths', []) or
                  any(path == directory or path.startswith(directory + '/')
                      for directory in inputs.get('directories', [])))
        if (not scoped or not isinstance(trusted, list) or len(trusted) != 3 or
                (trusted[0], trusted[1]) not in {('100644', 'blob'), ('100755', 'blob'), ('040000', 'tree')} or
                entries.get(path) != trusted or current_inventory.get(path) != trusted):
            raise ValueError('adapter_not_from_trusted_main')


def effective_policy(policy, request):
    """Variable adapter measurements still require exact evidence/request equality.

    Main approves each fixed runtime dimension and explicitly lists variable
    dimensions whose measurement code belongs to the reviewed input closure.
    The pure evaluator retains its exact three-way runtime comparison.
    """
    if 'runtime_basis' not in policy:
        return policy
    basis = policy.get('runtime_basis')
    variables = policy.get('runtime_variable_keys')
    runtime = request.get('runtime')
    if (not isinstance(basis, dict) or not basis or not isinstance(variables, list) or
            not all(isinstance(key, str) and key for key in variables) or
            len(set(variables)) != len(variables) or set(basis) & set(variables) or
            not isinstance(runtime, dict) or set(runtime) != set(basis) | set(variables) or
            any(reuse.canonical(runtime[key]) != reuse.canonical(value) for key, value in basis.items())):
        raise ValueError('runtime_basis_mismatch')
    result = dict(policy)
    result['runtime'] = runtime
    return result


def execution_policy(policy, worker_toml, current_inventory, blob):
    """Reconstruct the exact approved golden using immutable current lockfiles."""
    from pandora.config.loader import worker_table
    from pandora.engine.pinning import LOCKFILES, LOCKFILE_PATTERNS, golden_pins
    from pandora.engine.runner import toolchain_of
    from pandora.errors import ConfigError

    basis = policy.get('execution_context_basis')
    if (not isinstance(basis, dict) or set(basis) != {'image', 'lockfiles', 'env_digest', 'cwd'} or
            not isinstance(basis['image'], str) or not re.fullmatch(r'[0-9a-f]{64}', basis['image']) or
            not isinstance(basis['lockfiles'], list) or
            not all(isinstance(name, str) for name in basis['lockfiles']) or
            len(set(basis['lockfiles'])) != len(basis['lockfiles']) or
            not isinstance(basis['env_digest'], str) or not basis['env_digest'] or
            not isinstance(basis['cwd'], str) or
            'pandora.toml' not in policy.get('trusted_adapter_paths', [])):
        raise ValueError('invalid_execution_context_basis')
    found = {name for name in current_inventory if '/' not in name and
             (name in LOCKFILES or any(fnmatch.fnmatchcase(name, pattern) for pattern in LOCKFILE_PATTERNS))}
    if found != set(basis['lockfiles']):
        raise ValueError('lockfile_set_mismatch')
    inputs = policy.get('inputs', {})
    hashes = {}
    for name in sorted(found):
        entry = current_inventory[name]
        if (entry[:2] not in (['100644', 'blob'], ['100755', 'blob']) or
                not (inputs.get('root') is True or name in inputs.get('paths', []))):
            raise ValueError('lockfile_not_bound_regular_input')
        hashes[name] = hashlib.sha256(blob(name)).hexdigest()
    try:
        worker = worker_table(tomllib.loads(worker_toml)['worker'])
    except (ConfigError, KeyError, TypeError, ValueError):
        raise ValueError('invalid_trusted_worker_recipe') from None
    worker['pins'] = {'base_image': basis['image'],
                      **{'lockfile:' + name: digest for name, digest in hashes.items()}}
    result = dict(policy)
    result['execution_context'] = {'golden': toolchain_of(worker).fingerprint(),
                                   'golden_pins': golden_pins(worker),
                                   'env_digest': basis['env_digest'], 'cwd': basis['cwd']}
    return result


def candidate_refs(listing, jobs, tree):
    """Filter exact namespaces before applying the bounded lookup budget."""
    refs = set()
    prefix = verdicts.EXECUTION_PREFIX + '/'
    for line in listing.splitlines():
        columns = line.split()
        if len(columns) != 2 or not columns[1].startswith(prefix):
            continue
        ref = columns[1]
        parts = ref[len(prefix):].split('/')
        if (len(parts) == 3 and verdicts.TREE.fullmatch(parts[0]) and
                parts[1] in jobs and verdicts.RUN_ID.fullmatch(parts[2])):
            refs.add(ref)
    return sorted(refs, key=lambda ref: (not ref.startswith(prefix + tree + '/'), ref))


def plan(worktree, collection, *, default_branch, event, run_number, force_full=False):
    result = {'schema': 1, 'reusable': False, 'skip_units': [], 'remaining_units': [],
              'reason': 'reuse_unavailable', 'rejected': [], 'runs': [], 'coverage': {},
              'eligible_units': [], 'diagnostics': []}
    canary = False
    deadline = time.monotonic() + 90
    git = lambda *args: verdicts.git(worktree, *args, deadline=deadline)
    try:
        with Path(collection).open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('collection_too_large')
        request = json.loads(raw)
        wanted = reuse._units(request.get('units'))
        result['remaining_units'] = sorted(wanted)
        result['request'] = request
        if (type(force_full) is not bool or event not in ('pull_request', 'merge_group') or
                type(run_number) is not int or run_number <= 0):
            result['reason'] = 'force_full'
            return result
        if force_full:
            result['reason'] = 'force_full'
            return result
        canary = run_number % 10 == 0
        tree = git('rev-parse', 'HEAD^{tree}')
        if request.get('source_tree') != tree:
            raise ValueError('collection_tree_mismatch')
        check_id = request.get('check_id')
        if not isinstance(check_id, str) or not verdicts.JOB.fullmatch(check_id):
            raise ValueError('bad_check_id')
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', default_branch) or default_branch.startswith('-'):
            raise ValueError('bad_default_branch')
        trusted = 'refs/remotes/origin/pandora-execution-default'
        git('fetch', '--quiet', '--no-tags', '--depth=1', 'origin',
            '+refs/heads/%s:%s' % (default_branch, trusted))
        allowed = git('show', trusted + ':.github/pandora/allowed_signers')
        if not allowed or len(allowed) > 65536:
            raise ValueError('signers_unavailable')
        policy = json.loads(git('show', trusted + ':.github/pandora/execution-reuse.json'))['checks'][check_id]
        jobs = policy.get('worker_jobs')
        if jobs is None:
            job = policy.get('worker_job', check_id)
            jobs = {job: {'command': policy.get('command')}} if isinstance(job, str) else {}
        if (not isinstance(jobs, dict) or not jobs or
                any(not isinstance(job, str) or not verdicts.JOB.fullmatch(job) or
                    not isinstance(value, dict) or set(value) != {'command'} for job, value in jobs.items())):
            raise ValueError('invalid_worker_job')
        policy = effective_policy(policy, request)
        trusted_tree = git('rev-parse', trusted + '^{tree}')
        trusted_inventory = reuse.input_inventory(worktree, trusted_tree)['entries']
        current_inventory = reuse.input_inventory(worktree, tree)['entries']
        if 'execution_context_basis' in policy:
            def blob(path):
                identity = tree + ':' + path
                if int(git('cat-file', '-s', identity)) > 64 * 1024 * 1024:
                    raise ValueError('lockfile_too_large')
                return subprocess.run(['git', '-C', str(worktree), 'show', identity],
                    check=True, capture_output=True, timeout=10).stdout
            policy = execution_policy(policy, git('show', trusted + ':pandora.toml'), current_inventory, blob)
        patterns = [verdicts.EXECUTION_PREFIX + '/*/' + job + '/*' for job in sorted(jobs)]
        listing = git('ls-remote', '--', 'origin', *patterns)
        refs = candidate_refs(listing, jobs, tree)
        result['available_runs'] = len(refs)
        result['runs_truncated'] = len(refs) > MAX_RUNS
        skip = set()
        eligible = set()
        with tempfile.TemporaryDirectory(prefix='pandora-execution-') as folder:
            trust = Path(folder) / 'allowed_signers'
            trust.write_text(allowed + '\n')
            for index, ref in enumerate(refs[:MAX_RUNS]):
                local = 'refs/pandora-execution-ci/%d' % index
                try:
                    parts = ref.removeprefix(verdicts.EXECUTION_PREFIX + '/').split('/')
                    if (len(parts) != 3 or not verdicts.TREE.fullmatch(parts[0]) or
                            parts[1] not in jobs or not verdicts.RUN_ID.fullmatch(parts[2])):
                        raise ValueError('invalid_execution_ref')
                    source, job, run_id = parts
                    candidate_policy = dict(policy, command=jobs[job]['command'])
                    git('fetch', '--quiet', '--no-tags', '--depth=1', 'origin', '+' + ref + ':' + local)
                    content = {}
                    for name, limit in [('payload.json', MAX_PAYLOAD_BYTES), ('verdict.sig', 16384), ('report.json', MAX_BYTES)]:
                        if int(git('cat-file', '-s', local + ':' + name)) > limit:
                            raise ValueError('artifact_too_large')
                        content[name] = subprocess.run(['git', '-C', str(worktree), 'show', local + ':' + name],
                            check=True, capture_output=True, timeout=10).stdout
                    signature = Path(folder) / 'verdict.sig'
                    signature.write_bytes(content['verdict.sig'])
                    checked = subprocess.run(['ssh-keygen', '-Y', 'verify', '-f', str(trust),
                        '-I', 'pandora-verdict', '-n', 'pandora-verdict', '-s', str(signature)],
                        input=content['payload.json'], capture_output=True, timeout=10)
                    if checked.returncode:
                        raise ValueError('bad_signature')
                    evidence, body = authenticate(content['payload.json'], content['report.json'],
                        repo=request['repo'], tree=source, job=job, run_id=run_id, policy=candidate_policy)
                    verify_adapters(body, policy, trusted_inventory, current_inventory)
                    def manifest(identity, spec):
                        try:
                            return reuse.input_manifest(worktree, identity, spec)
                        except subprocess.CalledProcessError:
                            if identity != source:
                                raise
                            return reuse.attested_manifest(body.get('input_inventory'), identity, spec)
                    candidate_request = dict(request)
                    # CI observes its own runtime, not the worker engine. A null
                    # engine means use this authenticated, policy-approved signer
                    # engine solely for the evaluator's producer identity check.
                    if candidate_request.get('engine_identity') is None:
                        engine = body.get('engine')
                        if engine not in policy.get('approved_engines', []):
                            raise ValueError('engine_mismatch')
                        candidate_request['engine_identity'] = engine
                    decision = reuse.evaluate(evidence, candidate_request, policy, manifest=manifest,
                                              now=time.time(), canary=canary)
                    result['runs'].append({'run_id': run_id, 'reason': decision['reason'],
                                           'skip_units': decision['skip_units'], 'eligible_units': decision['eligible_units']})
                    eligible.update(decision['eligible_units'])
                    coverage = evidence.get('coverage')
                    if isinstance(coverage, dict):
                        for identity in decision['skip_units']:
                            if identity in coverage and identity not in skip:
                                result['coverage'][identity] = coverage[identity]
                    skip.update(decision['skip_units'])
                except (ValueError, KeyError, TypeError, AttributeError, OSError,
                        subprocess.SubprocessError, verdicts.Failed, RecursionError, OverflowError) as error:
                    result['rejected'].append({'ref': ref, 'reason': str(error) if isinstance(error, ValueError)
                                              else 'artifact_unavailable'})
                finally:
                    try:
                        git('update-ref', '-d', local)
                    except verdicts.Failed:
                        pass
        result.update(skip_units=sorted(skip), remaining_units=sorted(set(wanted) - skip),
                      reusable=bool(skip), eligible_units=sorted(eligible),
                      reason='canary' if canary else ('match' if skip else 'no_verified_evidence'))
    except (ValueError, KeyError, TypeError, AttributeError, OSError,
            subprocess.SubprocessError, verdicts.Failed, RecursionError, OverflowError) as error:
        reason = str(error) if isinstance(error, ValueError) else 'reuse_unavailable'
        result['reason'] = 'canary' if canary else reason
        result['diagnostics'].append(reason)
    return result


def diagnostic_summary(result):
    """Keep existing decisions intact; expose aggregate evidence and miss counts."""
    return {
        'available_runs': result.get('available_runs'),
        'runs_truncated': result.get('runs_truncated', False),
        'evaluated_runs': len(result['runs']),
        'eligible_units': len(result['eligible_units']),
        'reused_units': len(result['skip_units']),
        'remaining_units': len(result['remaining_units']),
        'rejections': dict(sorted(Counter(row['reason'] for row in result['rejected']).items())),
        'decisions': dict(sorted(Counter(row['reason'] for row in result['runs']).items())),
        'diagnostics': result['diagnostics'],
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collection', required=True)
    parser.add_argument('--default-branch', default='main')
    parser.add_argument('--event', default='')
    parser.add_argument('--run-number', type=int, default=0)
    parser.add_argument('--force-full', action='store_true')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = plan(Path.cwd(), args.collection, default_branch=args.default_branch,
                  event=args.event, run_number=args.run_number, force_full=args.force_full)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + '\n')
    print('Pandora execution reuse: %s; %d whole units reused.' % (result['reason'], len(result['skip_units'])))

    print('Pandora execution diagnostics: ' + json.dumps(diagnostic_summary(result), sort_keys=True))
