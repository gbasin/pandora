"""Opt-in, pre-test reuse of reviewed portable whole files. Any error runs tests."""
import copy
import json
import re
import time
from pathlib import Path

from ..engine.inventory import MAX_BYTES, identifiable_tests, identity, validate
from . import observations, verdicts

POLICY = '.github/pandora/unit-reuse.json'
COLLECTION = 'pandora-test-collection'


def compatible_profile(ci, worker, project):
    """Only the audited portability policy permits kernel and worker-count differences."""
    def relevant(profile):
        value = copy.deepcopy(profile)
        value.pop('os_release', None)
        projects = value.pop('projects', [])
        selected = [p for p in projects if isinstance(p, dict) and p.get('name') == project]
        if len(selected) != 1:
            raise ValueError('missing_project')
        value['project'] = selected[0]
        contract = value.get('reuse')
        if not isinstance(contract, dict) or contract.get('version') != 1:
            raise ValueError('missing_reuse_contract')
        configs = contract.pop('projects', [])
        selected = [p for p in configs if isinstance(p, dict) and p.get('name') == project]
        if len(selected) != 1:
            raise ValueError('missing_project_contract')
        contract['project'] = selected[0]
        if (value.get('platform') != 'linux' or contract.get('root_global_setup') != [] or
                value['project'].get('environment') != 'node' or
                value['project'].get('isolate') is not True or
                value['project'].get('setup_files') != [] or
                value['project'].get('global_setup') != []):
            raise ValueError('not_portable_isolated_unit')
        return value
    return relevant(ci) == relevant(worker)


def select(collection, candidates, allowed, *, tree):
    """Credit a file only from one complete run matching the entire CI collection."""
    ci = copy.deepcopy(collection)
    if (ci.get('kind') != COLLECTION or ci.get('complete') is not True or
            ci.get('outcome') != 'collected' or ci.get('errors')):
        raise ValueError('collection_not_completed')
    ci['kind'] = 'pandora-test-report'
    validate(ci, allow_empty=True)
    if any(t['status'] != 'pending' for t in ci['tests']):
        raise ValueError('collection_executed_tests')
    if ci['selection'] != {'name_pattern': None, 'reuse_safe_args': True}:
        raise ValueError('unsupported_selection')
    identifiable, _ = identifiable_tests(ci)
    by_file = {}
    for test in identifiable:
        by_file.setdefault((test['project'], test['file']), []).append(test)
    matches = {}
    for body, worker in candidates:
        observations.validate_passed(worker)
        if (body['tree'] != tree or worker['runner_version'] != ci['runner_version'] or
                worker['selection'] != {'name_pattern': None, 'reuse_safe_args': True}):
            continue
        worker_tests, _ = identifiable_tests(worker)
        modules = {(m['project'], m['file']) for m in worker['modules'] if m['state'] == 'passed'}
        for key, required in by_file.items():
            if key not in allowed or key not in modules or key in matches:
                continue
            actual = [t for t in worker_tests if (t['project'], t['file']) == key]
            if not required or not actual:
                continue
            if any(t['mode'] != 'run' or t['status'] != 'passed' or
                   t.get('retry_count') != 0 or t.get('repeat_count') != 0 for t in actual):
                continue
            if any(t['mode'] != 'run' for t in required):
                continue
            if {identity(t) for t in required} != {identity(t) for t in actual}:
                continue
            try:
                compatible = compatible_profile(ci['profile'], worker['profile'], key[0])
            except ValueError:
                continue
            if compatible:
                matches[key] = body['run_id']
    # An exclusion applies to a path in every project. Never drop another project.
    return [{'project': project, 'file': file, 'run_id': run}
            for (project, file), run in sorted(matches.items())
            if all(t['project'] == project for t in ci['tests'] if t['file'] == file)]


def plan(worktree, collection_path, *, repo, default_branch='main', event='', run_number=0):
    result = {'schema': 1, 'reason': 'reuse_unavailable', 'tree': '', 'skip_files': [],
              'eligible_files': [], 'rejected': [], 'canary': False}
    deadline = time.monotonic() + 90
    git = lambda *args: verdicts.git(worktree, *args, deadline=deadline)
    try:
        if event != 'pull_request':
            result['reason'] = 'pr_only'
            return result
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', default_branch) or default_branch.startswith('-'):
            raise ValueError('bad_default_branch')
        with Path(collection_path).open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('collection_too_large')
        collection = json.loads(raw)
        tree = git('rev-parse', 'HEAD^{tree}')
        result['tree'] = tree
        if collection.get('tree') != tree:
            raise ValueError('collection_tree_mismatch')
        if collection.get('complete') is True and collection.get('tests') == [] and not collection.get('errors'):
            result['reason'] = 'no_candidate_files'
            return result
        candidates, trusted = observations.verified_candidates(worktree, repo=repo, job='unit',
            lookup_tree=tree, default_branch=default_branch, deadline=deadline, summary=result)
        text = git('show', trusted + ':' + POLICY)
        if len(text) > 65536:
            raise ValueError('policy_too_large')
        policy = json.loads(text)
        if policy.get('schema') != 1 or policy.get('enabled') is not True:
            result['reason'] = 'policy_disabled'
            return result
        allowed = set()
        for entry in policy['portable_files']:
            file, project = entry['file'], entry['project']
            paths = entry['guard_paths']
            objects = entry['guard_objects']
            if entry.get('portability') != 'linux-node-isolated-v1':
                raise ValueError('unsupported_portability_policy')
            if (not isinstance(file, str) or not re.fullmatch(r'[A-Za-z0-9_./-]+', file) or
                    file.startswith(('/', '-')) or '..' in Path(file).parts or
                    not isinstance(project, str) or not paths or file not in paths):
                raise ValueError('invalid_policy')
            unchanged = True
            for path in paths:
                if (not isinstance(path, str) or not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or
                        path.startswith(('/', '-')) or '..' in Path(path).parts):
                    raise ValueError('invalid_guard')
                expected = objects.get(path)
                if not isinstance(expected, str) or not verdicts.TREE.fullmatch(expected):
                    raise ValueError('invalid_guard_object')
                if (git('rev-parse', 'HEAD:' + path) != expected or
                        git('rev-parse', trusted + ':' + path) != expected):
                    unchanged = False
                    break
            if unchanged:
                allowed.add((project, file))
        matches = select(collection, candidates, allowed, tree=tree)
        result['eligible_files'] = matches
        result['canary'] = bool(run_number and run_number % 10 == 0)
        if not result['canary']:
            result['skip_files'] = [m['file'] for m in matches]
        result['reason'] = 'canary_run' if result['canary'] else 'verified' if matches else 'no_eligible_evidence'
    except (ValueError, TypeError, KeyError, AttributeError, OSError,
            verdicts.Failed, RecursionError, OverflowError):
        # Never leave a partially authorized plan behind on a transport/policy error.
        result['skip_files'] = []
        result['reason'] = 'reuse_unavailable'
    return result
