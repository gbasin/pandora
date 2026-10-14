"""Authentication and conservative execution-plan boundaries."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[2] / 'scripts/execution-reuse-plan.py'
spec = importlib.util.spec_from_file_location('execution_reuse_plan', MODULE)
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


class ExecutionPlanTests(unittest.TestCase):
    def fixture(self):
        report = {'kind': 'pandora-execution-report', 'schema': 1, 'worker_run': 'run1',
                  'execution': {'schema': 1, 'complete': True, 'outcome': 'passed'}}
        raw = json.dumps(report).encode()
        context = {'golden': 'pin', 'golden_pins': {'node': 'v22'}, 'env_digest': 'env',
                   'argv': ['pnpm', 'native'], 'cwd': ''}
        body = {'kind': 'pandora-verdict', 'v': 1, 'outcome': 'passed', 'repo': 'repo',
                'tree': 'a' * 40, 'job': 'native', 'run_id': 'run1', 'engine': 'b' * 64,
                'finished': 100, 'source_integrity': True, 'test_evidence': {'bytes': len(raw),
                                                 'sha256': hashlib.sha256(raw).hexdigest()},
                **context}
        command = context.pop('argv')
        return body, raw, {'execution_context': context, 'command': command, 'inputs': {'root': True, 'paths': [], 'directories': []}}

    def auth(self, body, raw, policy):
        return planner.authenticate(json.dumps(body).encode(), raw, repo='repo',
                                    tree='a' * 40, job='native', run_id='run1', policy=policy)

    def test_authoritative_context_and_attested_inputs(self):
        body, raw, policy = self.fixture()
        with patch.object(planner.reuse, 'attested_manifest', return_value='c' * 64):
            evidence, _ = self.auth(body, raw, policy)
        self.assertEqual(evidence['engine_identity'], 'b' * 64)
        self.assertEqual(evidence['source_tree'], 'a' * 40)
        self.assertEqual(evidence['input_manifest'], 'c' * 64)
        self.assertEqual(evidence['inputs'], policy['inputs'])

    def test_unsigned_source_integrity_cannot_authorize_reuse(self):
        body, raw, policy = self.fixture()
        body.pop('source_integrity')
        with self.assertRaisesRegex(ValueError, 'source_integrity_unproven'):
            self.auth(body, raw, policy)

    def test_changed_bytes_and_execution_context_rejected(self):
        body, raw, policy = self.fixture()
        with self.assertRaisesRegex(ValueError, 'report_digest_mismatch'):
            self.auth(body, raw + b' ', policy)
        body['env_digest'] = 'other'
        with self.assertRaisesRegex(ValueError, 'execution_context_mismatch'):
            self.auth(body, raw, policy)

    def test_report_cannot_override_engine_context(self):
        body, raw, policy = self.fixture()
        report = json.loads(raw)
        report['execution']['engine_identity'] = 'd' * 64
        raw = json.dumps(report).encode()
        body['test_evidence'] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        with self.assertRaisesRegex(ValueError, 'report_context_conflict'):
            self.auth(body, raw, policy)

    def test_canaries_keep_all_work_and_explicit_overrides_never_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            collection = Path(directory) / 'request.json'
            collection.write_text(json.dumps({'units': [{'id': 'whole-file'}]}))
            with patch.object(planner.verdicts, 'git', side_effect=planner.verdicts.Failed('unavailable')):
                for event in ('pull_request', 'merge_group'):
                    result = planner.plan(directory, collection, default_branch='main', event=event, run_number=10)
                    self.assertEqual(result['reason'], 'canary')
                    self.assertEqual(result['remaining_units'], ['whole-file'])
                    self.assertFalse(result['skip_units'])
            with patch.object(planner.verdicts, 'git', side_effect=AssertionError('must not fetch')):
                for event, force in (('push', False), ('pull_request', True)):
                    result = planner.plan(directory, collection, default_branch='main', event=event,
                                          run_number=1, force_full=force)
                    self.assertEqual(result['remaining_units'], ['whole-file'])
                    self.assertFalse(result['reusable'])

    def test_exact_job_filter_precedes_cap_and_request_tree_is_first(self):
        prefix = planner.verdicts.EXECUTION_PREFIX
        tree = 'a' * 40
        matching = '%s/%s/unit/run1' % (prefix, tree)
        broad = ['hash %s/%s/native-unit/run%d' % (prefix, 'b' * 40, index)
                 for index in range(planner.MAX_RUNS + 10)]
        unrelated = 'hash %s/%s/unit/run2' % (prefix, 'c' * 40)
        selected = planner.candidate_refs('\n'.join(broad + [unrelated, 'hash ' + matching]), {'unit': {}}, tree)
        self.assertEqual(selected, [matching, unrelated.removeprefix('hash ')])

    def test_literal_command_units_reject_flags_globs_and_untracked_paths(self):
        command = {'prefix': ['pnpm', 'native'], 'suffix': [], 'unit_arguments': True}
        inputs = {'root': False, 'paths': ['test.py'], 'directories': []}
        inventory = {'entries': {'test.py': ['100644', 'blob', 'a' * 40]}}
        evidence = {'units': [{'id': 'test.py', 'command_arg': 'test.py'}]}
        planner.validate_command(['pnpm', 'native', 'test.py'], command, evidence, inventory, inputs)
        for argument in ('--grep', '*.py', 'missing.py', '../test.py'):
            with self.assertRaises(ValueError):
                planner.validate_command(['pnpm', 'native', argument], command, evidence, inventory, inputs)

    def test_trusted_app_prefix_normalizes_only_safe_literal_paths(self):
        command = {'prefix': ['pnpm', 'native'], 'suffix': [], 'unit_arguments': True,
                   'unit_path_prefix': 'apps/agent'}
        inputs = {'root': False, 'paths': [], 'directories': ['apps/agent']}
        inventory = {'entries': {'apps/agent/test.py': ['100644', 'blob', 'a' * 40]}}
        evidence = {'units': [{'id': 'apps/agent/test.py', 'command_arg': 'apps/agent/test.py'}]}
        planner.validate_command(['pnpm', 'native', 'test.py'], command, evidence, inventory, inputs)
        for argument in ('../test.py', '--runInBand', '*.py'):
            with self.assertRaises(ValueError):
                planner.validate_command(['pnpm', 'native', argument], command, evidence, inventory, inputs)

    def test_trusted_literal_selector_commands_reject_filters_and_patterns(self):
        command = {'prefix': ['node', 'tools/validation/remote-validation.mjs', 'arc'],
                   'suffix': [], 'literal_arguments': True}
        prefix = command['prefix']
        for argument in ('s0-03', 'tools/arc/s0-03.spec.mjs', 'agent:onboarding'):
            planner.validate_command(prefix + [argument], command, {}, {}, {})
        for argument in ('--grep', '*.spec.mjs', '../test', 'a/../b', '', 'name with space', 'a|b', 'a\n'):
            with self.assertRaisesRegex(ValueError, 'unsafe_literal_selector'):
                planner.validate_command(prefix + [argument], command, {}, {}, {})

    def test_literal_policy_allows_only_reviewed_exact_flags(self):
        command = {'prefix': ['node', 'wrapper.mjs', 'native', 'agent'], 'suffix': [],
                   'literal_arguments': True, 'allowed_flags': ['--runTestsByPath']}
        planner.validate_command(command['prefix'] + ['--runTestsByPath', 'test.ts'], command, {}, {}, {})
        with self.assertRaisesRegex(ValueError, 'unsafe_literal_selector'):
            planner.validate_command(command['prefix'] + ['--testNamePattern', 'name'], command, {}, {}, {})

    def test_missing_collection_has_no_skip_authority(self):
        result = planner.plan('.', '/missing/pandora-collection', default_branch='main',
                              event='pull_request', run_number=1)
        self.assertFalse(result['reusable'])
        self.assertEqual(result['skip_units'], [])


class ExecutionPlanIntegrationTests(unittest.TestCase):
    def setUp(self):
        import subprocess
        self.subprocess = subprocess
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.work = self.root / 'repo'
        self.work.mkdir()
        self.remote = self.root / 'origin.git'
        self.command('git', 'init', '--quiet', '--bare', str(self.remote))
        self.git('init', '--quiet', '-b', 'main')
        self.git('config', 'user.name', 'test')
        self.git('config', 'user.email', 'test@example.com')
        self.git('remote', 'add', 'origin', str(self.remote))
        self.key = self.root / 'key'
        self.command('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.key))
        trust = self.work / '.github/pandora'
        trust.mkdir(parents=True)
        trust.joinpath('allowed_signers').write_text('pandora-verdict ' + self.key.with_suffix('.pub').read_text())
        self.engine = 'b' * 64
        self.policy = {'schema': 1, 'enabled': True, 'repo': 'repo', 'family': 'native',
                       'check_id': 'native-agent', 'worker_job': 'native-unit',
                       'approved_engines': [self.engine], 'max_age_seconds': 3600,
                       'granularity': 'independent-unit',
                       'inputs': {'root': False, 'paths': ['adapter.py', 'test.py'], 'directories': []},
                       'required_paths': ['adapter.py'], 'trusted_adapter_paths': ['adapter.py'],
                       'runtime_basis': {'executor': {'image': 'fixed'}},
                       'runtime_variable_keys': ['config'],
                       'execution_context': {'golden': 'pin', 'golden_pins': {'node': 'v22'},
                                             'env_digest': 'env', 'cwd': ''},
                       'command': ['pnpm', 'native']}
        trust.joinpath('execution-reuse.json').write_text(json.dumps({'checks': {'native-agent': self.policy}}))
        self.work.joinpath('adapter.py').write_text('trusted adapter\n')
        self.work.joinpath('test.py').write_text('test\n')
        self.git('add', '.')
        self.git('commit', '--quiet', '-m', 'fixture')
        self.git('push', '--quiet', 'origin', 'main')
        self.tree = self.git('rev-parse', 'HEAD^{tree}').decode().strip()
        self.request = {'schema': 1, 'repo': 'repo', 'family': 'native', 'check_id': 'native-agent',
                        'source_tree': self.tree, 'engine_identity': None,
                        'runtime': {'executor': {'image': 'fixed'}, 'config': 'config-digest'},
                        'selection': {}, 'units': [{'id': 'test.py'}]}
        self.collection = self.root / 'collection.json'
        self.collection.write_text(json.dumps(self.request))

    def command(self, *argv, data=None):
        return self.subprocess.run(argv, input=data, check=True, capture_output=True).stdout

    def git(self, *argv, data=None):
        return self.command('git', '-C', str(self.work), *argv, data=data)

    def publish(self, *, engine=None, config='config-digest', corrupt_signature=False, absent_source=False, changed_adapter=False, worker_job='native-unit', context=None):
        import time
        evidence = {'schema': 1, 'family': 'native', 'check_id': 'native-agent',
                    'runtime': {'executor': {'image': 'fixed'}, 'config': config}, 'selection': {},
                    'complete': True, 'outcome': 'passed',
                    'coverage': {'test.py': {'observed': ['stage1']}, 'unrequested': {'observed': ['unsafe']}},
                    'units': [{'id': 'test.py', 'command_arg': 'test.py', 'complete': True,
                               'outcome': 'passed', 'attempts': [{'ordinal': 0, 'outcome': 'passed'}]}]}
        raw = json.dumps({'kind': 'pandora-execution-report', 'schema': 1,
                          'worker_run': 'run1', 'execution': evidence})
        inventory = planner.reuse.input_inventory(self.work, self.tree)
        source = 'd' * 40 if absent_source else self.tree
        inventory['source_tree'] = source
        if changed_adapter:
            inventory['entries']['adapter.py'][2] = 'e' * 40
            inventory['sha256'] = hashlib.sha256(planner.reuse.canonical(inventory['entries'])).hexdigest()
        body = {'kind': 'pandora-verdict', 'v': 1, 'outcome': 'passed', 'repo': 'repo',
                'tree': source, 'job': worker_job, 'run_id': 'run1',
                'engine': engine or self.engine, 'finished': time.time(), 'source_integrity': True,
                'argv': ['pnpm', 'native'], **(context or self.policy['execution_context']),
                'input_inventory': inventory,
                # A normal inventory exceeds the previous 64KiB envelope limit.
                'padding': 'x' * 70000,
                'test_evidence': {'bytes': len(raw.encode()), 'sha256': hashlib.sha256(raw.encode()).hexdigest()}}
        payload = planner.reuse.canonical(body)
        signature_file = self.root / 'payload'
        signature_file.with_suffix('.sig').unlink(missing_ok=True)
        signature_file.write_bytes(payload)
        self.command('ssh-keygen', '-q', '-Y', 'sign', '-f', str(self.key), '-n', 'pandora-verdict', str(signature_file))
        signature = signature_file.with_suffix('.sig').read_text()
        if corrupt_signature:
            signature = 'invalid signature\n'
        commit = planner.verdicts.build(self.work, self.tree, 'native-unit', payload.decode(), signature,
                                        self.key.with_suffix('.pub').read_text(), report=raw)
        ref = '%s/%s/%s/run1' % (planner.verdicts.EXECUTION_PREFIX, source, worker_job)
        self.git('push', '--quiet', 'origin', commit + ':' + ref)

    def decide(self):
        return planner.plan(self.work, self.collection, default_branch='main', event='pull_request', run_number=1)

    def test_signed_large_payload_worker_job_and_null_engine(self):
        self.publish()
        result = self.decide()
        self.assertEqual(result['skip_units'], ['test.py'], result)
        self.assertEqual(result['remaining_units'], [])
        self.assertIsNone(result['request']['engine_identity'])
        self.assertEqual(result['coverage'], {'test.py': {'observed': ['stage1']}})

    def test_absent_synthetic_source_uses_only_signed_inventory(self):
        self.publish(absent_source=True)
        result = self.decide()
        self.assertEqual(result['skip_units'], ['test.py'], result)
        self.assertEqual(result['coverage'], {'test.py': {'observed': ['stage1']}})

    def test_unknown_engine_never_gets_candidates(self):
        self.publish(engine='c' * 64)
        result = self.decide()
        self.assertEqual(result['skip_units'], [])
        self.assertEqual(result['remaining_units'], ['test.py'])
        self.assertEqual(result['rejected'][0]['reason'], 'engine_mismatch')

    def test_bad_signature_falls_back(self):
        self.publish(corrupt_signature=True)
        result = self.decide()
        self.assertEqual(result['skip_units'], [])
        self.assertEqual(result['rejected'][0]['reason'], 'bad_signature')

    def test_runtime_variable_still_requires_exact_report_match(self):
        self.publish(config='other')
        result = self.decide()
        self.assertEqual(result['skip_units'], [])
        self.assertEqual(result['runs'][0]['reason'], 'runtime_mismatch')

    def test_unapproved_runtime_basis_and_extra_dimensions_rejected(self):
        for runtime in ({'executor': {'image': 'unapproved'}, 'config': 'config-digest'},
                        {'executor': {'image': 'fixed'}, 'config': 'config-digest', 'unsafe': True}):
            with self.assertRaisesRegex(ValueError, 'runtime_basis_mismatch'):
                planner.effective_policy(self.policy, {'runtime': runtime})

    def test_candidate_branch_cannot_approve_its_own_runtime(self):
        self.publish()
        policy = dict(self.policy)
        policy['runtime_basis'] = {'executor': {'image': 'unapproved'}}
        self.work.joinpath('.github/pandora/execution-reuse.json').write_text(
            json.dumps({'checks': {'native-agent': policy}}))
        # This checkout change must never replace the fetched main policy.
        result = self.decide()
        self.assertEqual(result['skip_units'], ['test.py'], result)

    def test_pr_reporter_cannot_attest_its_own_semantics(self):
        self.publish(changed_adapter=True)
        result = self.decide()
        self.assertEqual(result['skip_units'], [])
        self.assertEqual(result['remaining_units'], ['test.py'])
        self.assertEqual(result['rejected'][0]['reason'], 'adapter_not_from_trusted_main')

    def test_ci_collector_and_runner_code_must_also_match_main(self):
        self.publish()
        self.work.joinpath('adapter.py').write_text('untrusted collector\n')
        self.git('add', 'adapter.py')
        self.git('commit', '--quiet', '-m', 'change collector')
        self.request['source_tree'] = self.git('rev-parse', 'HEAD^{tree}').decode().strip()
        self.collection.write_text(json.dumps(self.request))
        result = self.decide()
        self.assertEqual(result['skip_units'], [])
        self.assertEqual(result['rejected'][0]['reason'], 'adapter_not_from_trusted_main')

    def test_trusted_multiple_worker_jobs_and_unknown_job_rejection(self):
        self.policy.pop('worker_job')
        self.policy['worker_jobs'] = {'native-unit': {'command': ['pnpm', 'native']},
                                      'native-full': {'command': ['pnpm', 'native']}}
        policy_path = self.work / '.github/pandora/execution-reuse.json'
        policy_path.write_text(json.dumps({'checks': {'native-agent': self.policy}}))
        self.git('add', str(policy_path))
        self.git('commit', '--quiet', '-m', 'approve producer jobs')
        self.git('push', '--quiet', 'origin', 'main')
        self.tree = self.git('rev-parse', 'HEAD^{tree}').decode().strip()
        self.request['source_tree'] = self.tree
        self.collection.write_text(json.dumps(self.request))
        self.publish(worker_job='native-unit')
        self.publish(worker_job='native-full')
        self.publish(worker_job='unknown')
        result = self.decide()
        self.assertEqual(result['skip_units'], ['test.py'], result)
        self.assertEqual(result['available_runs'], 2)
        self.assertEqual(len(result['runs']), 2)

    def test_canary_retains_authenticated_eligibility_without_skip_or_coverage(self):
        self.publish()
        for event in ('pull_request', 'merge_group'):
            result = planner.plan(self.work, self.collection, default_branch='main', event=event, run_number=10)
            self.assertEqual(result['reason'], 'canary')
            self.assertEqual(result['eligible_units'], ['test.py'], result)
            self.assertEqual(result['remaining_units'], ['test.py'])
            self.assertFalse(result['reusable'])
            self.assertEqual(result['skip_units'], [])
            self.assertEqual(result['coverage'], {})

    def test_lockfile_renewal_reconstructs_golden_without_policy_edits(self):
        self.work.joinpath('pandora.toml').write_text('[worker]\nbase_image = "images:ubuntu/26.04"\n')
        self.work.joinpath('pnpm-lock.yaml').write_text('lock v1\n')
        self.policy.pop('execution_context')
        self.policy['execution_context_basis'] = {'image': 'a' * 64, 'lockfiles': ['pnpm-lock.yaml'],
                                                   'env_digest': 'env', 'cwd': ''}
        self.policy['trusted_adapter_paths'].append('pandora.toml')
        self.policy['required_paths'].append('pandora.toml')
        self.policy['inputs']['paths'].extend(['pandora.toml', 'pnpm-lock.yaml'])
        self.work.joinpath('.github/pandora/execution-reuse.json').write_text(
            json.dumps({'checks': {'native-agent': self.policy}}))
        self.git('add', '.')
        self.git('commit', '--quiet', '-m', 'approve golden basis')
        self.git('push', '--quiet', 'origin', 'main')
        self.tree = self.git('rev-parse', 'HEAD^{tree}').decode().strip()
        self.request['source_tree'] = self.tree
        self.collection.write_text(json.dumps(self.request))

        def context():
            inventory = planner.reuse.input_inventory(self.work, self.tree)['entries']
            return planner.execution_policy(self.policy, self.work.joinpath('pandora.toml').read_text(),
                inventory, lambda path: self.git('show', self.tree + ':' + path))['execution_context']

        original = context()
        self.publish(context=original)
        self.assertEqual(self.decide()['skip_units'], ['test.py'])
        self.work.joinpath('pnpm-lock.yaml').write_text('lock v2\n')
        self.git('add', 'pnpm-lock.yaml')
        self.git('commit', '--quiet', '-m', 'update dependencies')
        self.tree = self.git('rev-parse', 'HEAD^{tree}').decode().strip()
        self.request['source_tree'] = self.tree
        self.collection.write_text(json.dumps(self.request))
        result = self.decide()
        self.assertEqual(result['skip_units'], [])
        self.assertEqual(result['rejected'][0]['reason'], 'execution_context_mismatch')
        current = context()
        self.assertNotEqual(original['golden'], current['golden'])
        self.publish(context=current)
        result = self.decide()
        self.assertEqual(result['skip_units'], ['test.py'], result)
        self.assertEqual(len(result['runs']), 1)

    def test_golden_basis_rejects_unknown_lockfiles_and_unprotected_recipe(self):
        basis = {'image': 'a' * 64, 'lockfiles': [], 'env_digest': 'env', 'cwd': ''}
        policy = {'execution_context_basis': basis, 'trusted_adapter_paths': ['pandora.toml'],
                  'inputs': {'root': False, 'paths': ['pnpm-lock.yaml']}}
        inventory = {'pnpm-lock.yaml': ['100644', 'blob', 'b' * 40]}
        with self.assertRaisesRegex(ValueError, 'lockfile_set_mismatch'):
            planner.execution_policy(policy, '[worker]\nbase_image="image"\n', inventory, lambda _: b'lock')
        basis['lockfiles'] = ['pnpm-lock.yaml']
        policy['trusted_adapter_paths'] = []
        with self.assertRaisesRegex(ValueError, 'invalid_execution_context_basis'):
            planner.execution_policy(policy, '[worker]\nbase_image="image"\n', inventory, lambda _: b'lock')
