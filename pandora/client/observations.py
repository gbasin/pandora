"""Read worker observations after CI executes. No output can authorize a skip."""
import hashlib
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from ..engine.inventory import MAX_BYTES, identity, validate
from . import verdicts

MAX_RUNS = 32


def compare(ci, candidates, *, ci_tree):
    validate(ci)
    required = {identity(test): test for test in ci['tests'] if test['mode'] not in ('skip', 'todo')}
    files = {}
    for key in required:
        files.setdefault(key[:2], set()).add(key)
    observed_files, compatible_files = set(), set()
    observations = []
    for body, report in candidates:
        validate(report)
        # A signature authenticates what the worker collected, not runner semantics.
        if (report.get('complete') is not True or report.get('outcome') != 'passed' or
                report['errors'] or any(test['status'] in ('failed', 'pending') or
                test['mode'] == 'only' for test in report['tests'])):
            raise ValueError('report_not_passed')
        passed = {identity(test) for test in report['tests'] if test['status'] == 'passed'}
        modules = {(module['project'], module['file']) for module in report['modules']
                   if module['state'] == 'passed'}
        # Whole-file observations must come from one unfiltered execution.
        complete = ({file for file, cases in files.items() if cases <= passed and file in modules}
                    if report['selection'].get('name_pattern', 'unknown') is None else set())
        reasons = []
        if body['tree'] != ci_tree:
            reasons.append('ci_tree_mismatch')
        if report['runner_version'] != ci['runner_version']:
            reasons.append('runner_version_mismatch')
        profile_keys = sorted(key for key in report['profile'].keys() | ci['profile'].keys()
                              if report['profile'].get(key) != ci['profile'].get(key))
        if profile_keys:
            reasons.append('profile_mismatch')
        if ci.get('complete') is not True or ci.get('outcome') != 'passed' or ci['errors']:
            reasons.append('ci_not_passed')
        observed_files.update(complete)
        if not reasons:
            compatible_files.update(complete)
        observations.append({'run_id': body['run_id'], 'overlapping_complete_files': len(complete),
                             'matching_passed_cases': len(required.keys() & passed),
                             'incompatibilities': reasons, 'profile_differences': profile_keys,
                             'worker_execution_seconds': body['test_evidence'].get('execution_seconds')})
    case_ms = sum(test.get('duration_ms') or 0 for key, test in required.items()
                  if key[:2] in observed_files)
    return {'required_files': len(files), 'required_cases': len(required),
            'observed_complete_files': len(observed_files),
            'matching_profile_and_tree_files': len(compatible_files),
            'ci_case_duration_ms_in_observed_files': round(case_ms, 3),
            'timing_limit': 'Case durations overlap in parallel runs; this is not wall-clock savings.',
            'observations': observations}


def measure(worktree, ci_report, *, repo, job='unit', head_sha='', default_branch='main',
            signers='.github/pandora/allowed_signers'):
    summary = {'schema': 1, 'mode': 'measurement_only', 'skip_enabled': False,
               'reason': 'no_evidence', 'rejected': [], 'observations': []}
    deadline = time.monotonic() + 90
    git = lambda *args: verdicts.git(worktree, *args, deadline=deadline)
    try:
        if not verdicts.JOB.fullmatch(job) or not re.fullmatch(r'[A-Za-z0-9_./-]+', default_branch) or default_branch.startswith('-'):
            raise ValueError('bad_input')
        if not isinstance(repo, str) or not repo:
            raise ValueError('bad_input')
        if head_sha and not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head_sha):
            raise ValueError('bad_input')
        with Path(ci_report).open('rb') as stream:
            raw_ci = stream.read(MAX_BYTES + 1)
        if len(raw_ci) > MAX_BYTES:
            raise ValueError('ci_report_too_large')
        ci = validate(json.loads(raw_ci))
        ci_tree = git('rev-parse', 'HEAD^{tree}')
        if head_sha:
            try:
                lookup_tree = git('rev-parse', head_sha + '^{tree}')
            except verdicts.Failed:
                git('fetch', '--quiet', '--no-tags', '--depth=1', 'origin', head_sha)
                lookup_tree = git('rev-parse', head_sha + '^{tree}')
        else:
            lookup_tree = ci_tree
        summary.update(ci_tree=ci_tree, lookup_tree=lookup_tree,
                       source_relation='same_tree' if ci_tree == lookup_tree else 'head_observation_vs_ci_merge')
        if not verdicts.TREE.fullmatch(lookup_tree):
            raise ValueError('bad_tree')
        trusted_ref = 'refs/remotes/origin/pandora-evidence-default'
        git('fetch', '--quiet', '--no-tags', '--depth=1', 'origin',
            '+refs/heads/%s:%s' % (default_branch, trusted_ref))
        # Public trust policy is always from the default branch, never PR content.
        allowed = git('show', trusted_ref + ':' + signers)
        if not allowed or len(allowed) > 65536:
            raise ValueError('signers_unavailable')
        prefix = '%s/%s/%s/' % (verdicts.EVIDENCE_PREFIX, lookup_tree, job)
        listing = git('ls-remote', '--', 'origin', prefix + '*')
        refs = sorted(line.split()[1] for line in listing.splitlines() if len(line.split()) == 2)
        summary['available_runs'] = len(refs)
        summary['runs_truncated'] = len(refs) > MAX_RUNS
        candidates = []
        with tempfile.TemporaryDirectory(prefix='pandora-evidence-') as folder:
            trust = Path(folder) / 'allowed_signers'
            trust.write_text(allowed + '\n')
            for index, ref in enumerate(refs[:MAX_RUNS]):
                run_id = ref.removeprefix(prefix)
                if not ref.startswith(prefix) or not verdicts.RUN_ID.fullmatch(run_id):
                    summary['rejected'].append({'reason': 'invalid_ref'})
                    continue
                try:
                    local = 'refs/pandora-shadow/%d' % index
                    git('fetch', '--quiet', '--no-tags', '--depth=1', 'origin', '+' + ref + ':' + local)
                    content = {}
                    for name, limit in [('payload.json', 65536), ('verdict.sig', 16384), ('report.json', MAX_BYTES)]:
                        if int(git('cat-file', '-s', local + ':' + name)) > limit:
                            raise ValueError('artifact_too_large')
                        content[name] = git('show', local + ':' + name)
                    # git() strips outer whitespace; published payload is canonical JSON.
                    payload = content['payload.json']
                    signature = Path(folder) / 'verdict.sig'
                    signature.write_text(content['verdict.sig'] + '\n')
                    verified = subprocess.run(['ssh-keygen', '-Y', 'verify', '-f', str(trust),
                                              '-I', 'pandora-verdict', '-n', 'pandora-verdict',
                                              '-s', str(signature)], input=payload.encode(),
                                              capture_output=True, timeout=10)
                    if verified.returncode:
                        raise ValueError('bad_signature')
                    body = json.loads(payload)
                    if (body.get('kind') != 'pandora-verdict' or type(body.get('v')) is not int or
                            body['v'] != 1 or body.get('outcome') != 'passed' or
                            body.get('tree') != lookup_tree or body.get('job') != job or
                            body.get('repo') != repo or body.get('run_id') != run_id):
                        raise ValueError('verdict_context_mismatch')
                    binding = body.get('test_evidence') or {}
                    # Report bytes must survive exactly. The text helper above strips,
                    # so get the blob with a byte-preserving subprocess for the digest.
                    raw = subprocess.run(['git', '-C', str(worktree), 'show', local + ':report.json'],
                                         check=True, capture_output=True, timeout=10).stdout
                    if len(raw) != binding.get('bytes') or hashlib.sha256(raw).hexdigest() != binding.get('sha256'):
                        raise ValueError('report_digest_mismatch')
                    report = validate(json.loads(raw))
                    if report.get('worker_run') != run_id:
                        raise ValueError('report_run_mismatch')
                    # Validate each candidate alone so a bad artifact does not hide good runs.
                    compare(ci, [(body, report)], ci_tree=ci_tree)
                    candidates.append((body, report))
                except (ValueError, TypeError, KeyError, AttributeError, OSError,
                        subprocess.SubprocessError, verdicts.Failed) as error:
                    reason = str(error) if isinstance(error, ValueError) else 'artifact_unavailable'
                    summary['rejected'].append({'run_id': run_id, 'reason': reason})
                finally:
                    try:
                        git('update-ref', '-d', local)
                    except verdicts.Failed:
                        pass
        summary.update(compare(ci, candidates, ci_tree=ci_tree))
        summary['reason'] = 'observed' if candidates else 'no_verified_evidence'
    except FileNotFoundError:
        summary['reason'] = 'ci_report_missing'
    except (ValueError, TypeError, KeyError, AttributeError, OSError,
            subprocess.SubprocessError, verdicts.Failed):
        summary['reason'] = 'measurement_unavailable'
    return summary
