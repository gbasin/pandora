"""The e2e runner's job-start hook and the host pool that keeps one-job VMs."""
import importlib.util
import io
import ipaddress
import json
import signal
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from urllib.error import HTTPError

HERE = Path(__file__).resolve().parents[2] / 'deploy' / 'e2e-runner'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


trusted = load('e2e_trusted_job', HERE / 'trusted_job.py')
pool = load('e2e_pool', HERE / 'e2e_pool.py')

BASE = {'id': 1001, 'full_name': 'gbasin/pandora'}
FORK = {'id': 2002, 'full_name': 'mallory/pandora'}


class TrustedJobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='e2e-hook-')
        self.addCleanup(self.tmp.cleanup)

    def env(self, event, payload, **extra):
        path = Path(self.tmp.name) / 'event.json'
        path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
        env = {'GITHUB_EVENT_NAME': event, 'GITHUB_REPOSITORY': 'gbasin/pandora',
               'GITHUB_REPOSITORY_ID': '1001', 'GITHUB_EVENT_PATH': str(path),
               'GITHUB_ACTOR': 'gbasin', 'GITHUB_TRIGGERING_ACTOR': 'gbasin'}
        env.update(extra)
        return env

    def pull(self, head_repo, author='gbasin'):
        return {'pull_request': {'head': {'repo': head_repo}, 'user': {'login': author}}}

    def test_base_repository_events_are_admitted(self):
        for event in ('push', 'schedule', 'workflow_dispatch'):
            self.assertIsNone(trusted.verdict(self.env(event, {})), event)

    def test_a_same_repository_pull_request_is_admitted(self):
        self.assertIsNone(trusted.verdict(self.env('pull_request', self.pull(BASE))))

    def test_a_fork_pull_request_is_refused_by_id_even_with_a_matching_name(self):
        spoof = {'id': 2002, 'full_name': 'gbasin/pandora'}
        reason = trusted.verdict(self.env('pull_request', self.pull(spoof)))
        self.assertIn('head repository', reason)
        reason = trusted.verdict(self.env('pull_request', self.pull(FORK)))
        self.assertIn('mallory/pandora', reason)

    def test_names_decide_when_the_repository_id_is_unknown(self):
        env = self.env('pull_request', self.pull({'full_name': 'GBasin/Pandora'}),
                       GITHUB_REPOSITORY_ID='')
        self.assertIsNone(trusted.verdict(env))
        env = self.env('pull_request', self.pull({'full_name': 'mallory/pandora'}),
                       GITHUB_REPOSITORY_ID='')
        self.assertIsNotNone(trusted.verdict(env))

    def test_a_pull_request_with_no_head_repository_is_refused(self):
        self.assertIsNotNone(trusted.verdict(self.env('pull_request', self.pull(None))))
        self.assertIsNotNone(trusted.verdict(self.env('pull_request', {})))

    def test_dependabot_is_refused(self):
        env = self.env('pull_request', self.pull(BASE, author='dependabot[bot]'))
        self.assertIn('author', trusted.verdict(env))
        env = self.env('push', {}, GITHUB_ACTOR='dependabot[bot]')
        self.assertIn('GITHUB_ACTOR', trusted.verdict(env))

    def test_events_that_can_check_out_fork_code_are_refused(self):
        for event in ('pull_request_target', 'workflow_run', 'issue_comment'):
            self.assertIn('not admitted', trusted.verdict(self.env(event, self.pull(BASE))))

    def test_another_repository_is_refused(self):
        env = self.env('push', {}, GITHUB_REPOSITORY='mallory/pandora')
        self.assertIn('mallory/pandora', trusted.verdict(env))

    def test_a_missing_or_malformed_payload_is_refused(self):
        self.assertIsNotNone(trusted.verdict(self.env('push', 'not json')))
        self.assertIsNotNone(trusted.verdict(self.env('push', '[]')))
        env = self.env('push', {})
        env['GITHUB_EVENT_PATH'] = str(Path(self.tmp.name) / 'absent.json')
        self.assertIsNotNone(trusted.verdict(env))
        self.assertIsNotNone(trusted.verdict({}))

    def test_main_refuses_on_a_verdict_or_an_unforeseen_error(self):
        refused = []
        self.assertEqual(trusted.main(self.env('push', {}), refusal=refused.append), 0)
        self.assertEqual(refused, [])
        trusted.main(self.env('pull_request', self.pull(FORK)), refusal=refused.append)
        self.assertEqual(len(refused), 1)
        deep = '[' * 100000 + ']' * 100000
        trusted.main(self.env('pull_request', deep), refusal=refused.append)
        self.assertEqual(len(refused), 2)

    def test_refusal_kills_every_runner_process_but_never_everything_as_root(self):
        calls = []
        kill = lambda pid, sig: calls.append(('kill', pid, sig))
        killpg = lambda pgid, sig: calls.append(('killpg', pgid, sig))
        out = io.StringIO()
        with unittest.mock.patch('sys.stdout', out):
            trusted.refuse('fork', sleep=lambda s: None, kill=kill, killpg=killpg,
                           euid=lambda: 1002, own_group=lambda: 77)
        self.assertEqual(calls[0], ('kill', -1, signal.SIGKILL))
        self.assertIn('::error::', out.getvalue())
        calls.clear()
        with unittest.mock.patch('sys.stdout', io.StringIO()):
            trusted.refuse('fork', sleep=lambda s: None, kill=kill, killpg=killpg,
                           euid=lambda: 0, own_group=lambda: 77)
        self.assertEqual(calls, [('killpg', 77, signal.SIGKILL)])


class FakeIncus:
    def __init__(self):
        self.instances = {}
        self.deleted = []
        self.started = {}
        self.fail_start = False

    def vms(self):
        return [{'name': name, 'status': 'Running', 'config': dict(config)}
                for name, config in self.instances.items()]

    def launch(self, image, name, profile, now):
        self.instances[name] = {pool.CREATED: str(now)}

    def agent_ready(self, name):
        return True

    def start_runner(self, name, jit):
        if self.fail_start:
            raise pool.PoolError('start-runner failed')
        self.started[name] = jit

    def mark(self, name, key, now):
        self.instances[name][key] = str(now)

    def delete(self, name):
        self.deleted.append(name)
        self.instances.pop(name, None)


class FakeGitHub:
    def __init__(self):
        self.registered = {}
        self.removed = []
        self.next_id = 1

    def runners(self):
        return {name: dict(runner) for name, runner in self.registered.items()}

    def jit(self, name):
        self.registered[name] = {'id': self.next_id, 'name': name, 'busy': False}
        self.next_id += 1
        return 'Zm9v'

    def remove(self, runner):
        self.removed.append(runner['name'])
        self.registered.pop(runner['name'], None)


class PoolTest(unittest.TestCase):
    def setUp(self):
        self.now = 10_000
        self.incus, self.github = FakeIncus(), FakeGitHub()
        self.config = dict(pool.DEFAULTS)
        self.log = []
        self.pool = pool.Pool(self.config, self.incus, self.github, clock=lambda: self.now,
                              sleep=lambda s: None, say=self.log.append)

    def test_an_empty_pool_launches_one_runner_with_its_own_jit_config(self):
        status = self.pool.step()
        self.assertEqual(len(status['launched']), 1)
        (name,) = status['launched']
        self.assertRegex(name, pool.NAME)
        self.assertEqual(self.incus.started[name], 'Zm9v')
        self.assertIn(pool.STARTED, self.incus.instances[name])
        self.assertEqual(self.pool.step()['launched'], [])

    def test_a_busy_runner_is_marked_and_a_second_vm_fills_the_idle_slot(self):
        (name,) = self.pool.step()['launched']
        self.github.registered[name]['busy'] = True
        self.now += 30
        status = self.pool.step()
        self.assertEqual(self.incus.instances[name][pool.BUSY], str(self.now))
        self.assertEqual(status['busy'], 1)
        self.assertEqual(len(status['launched']), 1)
        self.now += 30
        self.assertEqual(self.pool.step()['launched'], [], 'max_vms is 2')

    def test_a_finished_job_retires_its_vm_and_refills(self):
        (name,) = self.pool.step()['launched']
        self.github.registered[name]['busy'] = True
        self.pool.step()
        self.github.registered.pop(name)
        self.now += 5
        self.pool.step()
        self.assertIn(name, self.incus.deleted)
        self.assertEqual(len(self.incus.instances), 1, 'the second VM is the idle one')

    def test_a_job_past_its_limit_is_retired(self):
        (name,) = self.pool.step()['launched']
        self.github.registered[name]['busy'] = True
        self.pool.step()
        self.now += self.config['job_max_seconds'] + 1
        self.pool.step()
        self.assertIn(name, self.incus.deleted)

    def test_a_vm_whose_runner_never_started_is_retired(self):
        self.incus.instances['e2e-0123456789ab'] = {pool.CREATED: str(self.now)}
        self.now += self.config['start_timeout_seconds'] + 1
        self.pool.step()
        self.assertIn('e2e-0123456789ab', self.incus.deleted)

    def test_a_vm_whose_registration_vanished_before_a_job_is_retired_after_grace(self):
        (name,) = self.pool.step()['launched']
        self.github.registered.pop(name)
        self.now += 10
        self.pool.step()
        self.assertNotIn(name, self.incus.deleted)
        self.now += self.config['gone_grace_seconds']
        self.pool.step()
        self.assertIn(name, self.incus.deleted)

    def test_an_idle_runner_is_recycled_after_idle_max(self):
        (name,) = self.pool.step()['launched']
        self.now += self.config['idle_max_seconds'] + 1
        self.pool.step()
        self.assertIn(name, self.incus.deleted)
        self.assertIn(name, self.github.removed)

    def test_a_registration_with_no_vm_is_deregistered(self):
        self.github.registered['e2e-aaaaaaaaaaaa'] = {'id': 9, 'name': 'e2e-aaaaaaaaaaaa',
                                                      'busy': False}
        self.pool.step()
        self.assertIn('e2e-aaaaaaaaaaaa', self.github.removed)

    def test_a_failed_start_deletes_the_vm_and_its_registration(self):
        self.incus.fail_start = True
        status = self.pool.step()
        self.assertEqual(status['launched'], [])
        self.assertEqual(self.incus.instances, {})
        self.assertEqual(self.github.registered, {})

    def test_foreign_instances_are_never_touched(self):
        incus = pool.Incus('pandora-e2e', run=lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, json.dumps([{'name': 'e2ebuild-1'}, {'name': 'compute-control'},
                                 {'name': 'e2e-0123456789ab'}]).encode(), b''))
        self.assertEqual([vm['name'] for vm in incus.vms()], ['e2e-0123456789ab'])

    def test_drain_spares_busy_runners(self):
        (name,) = self.pool.step()['launched']
        self.github.registered[name]['busy'] = True
        self.pool.drain()
        self.assertNotIn(name, self.incus.deleted)
        self.github.registered[name]['busy'] = False
        self.pool.drain()
        self.assertIn(name, self.incus.deleted)


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class GitHubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='e2e-gh-')
        self.addCleanup(self.tmp.cleanup)
        self.token = Path(self.tmp.name) / 'token'
        self.token.write_text('github_pat_secret\n')
        self.requests = []

    def client(self, reply):
        def opener(request, timeout):
            self.requests.append(request)
            if isinstance(reply, Exception):
                raise reply
            return Response(json.dumps(reply).encode() if reply is not None else b'')
        return pool.GitHub('gbasin/pandora', str(self.token), ['self-hosted', 'e2e'],
                           opener=opener)

    def test_jit_mints_one_named_runner_with_the_fixed_labels(self):
        config = self.client({'encoded_jit_config': 'YWJj'}).jit('e2e-0123456789ab')
        self.assertEqual(config, 'YWJj')
        (request,) = self.requests
        self.assertEqual(request.full_url, 'https://api.github.com/repos/gbasin/pandora'
                                           '/actions/runners/generate-jitconfig')
        self.assertEqual(json.loads(request.data),
                         {'name': 'e2e-0123456789ab', 'runner_group_id': 1,
                          'labels': ['self-hosted', 'e2e'], 'work_folder': '_work'})
        self.assertEqual(request.get_header('Authorization'), 'Bearer github_pat_secret')

    def test_jit_refuses_a_foreign_name_and_an_invalid_reply(self):
        with self.assertRaises(pool.PoolError):
            self.client({'encoded_jit_config': 'YWJj'}).jit('worker-e2e')
        self.assertEqual(self.requests, [])
        with self.assertRaises(pool.PoolError):
            self.client({'encoded_jit_config': 'a b; rm'}).jit('e2e-0123456789ab')

    def test_listing_keeps_only_pool_runners(self):
        runners = self.client({'runners': [{'id': 1, 'name': 'worker-e2e'},
                                           {'id': 2, 'name': 'e2e-0123456789ab'}]}).runners()
        self.assertEqual(list(runners), ['e2e-0123456789ab'])

    def test_remove_refuses_a_runner_the_pool_does_not_own(self):
        with self.assertRaises(pool.PoolError):
            self.client(None).remove({'id': 1, 'name': 'worker-e2e'})
        self.assertEqual(self.requests, [])
        self.client(None).remove({'id': 7, 'name': 'e2e-0123456789ab'})
        self.assertEqual(self.requests[0].get_method(), 'DELETE')
        self.assertTrue(self.requests[0].full_url.endswith('/actions/runners/7'))

    def test_errors_never_carry_the_token(self):
        error = HTTPError('https://api.github.com', 401, 'bad', {}, io.BytesIO(b''))
        with self.assertRaises(pool.PoolError) as caught:
            self.client(error).runners()
        self.assertNotIn('secret', str(caught.exception))


class AclTest(unittest.TestCase):
    def test_the_host_and_every_private_range_are_unreachable(self):
        rule = pool.acl(['5.135.138.35'])['egress'][0]
        nets = [ipaddress.ip_network(n) for n in rule['destination'].split(',')]
        for address in ('5.135.138.35', '10.4.79.1', '10.141.0.1', '10.142.0.1',
                        '169.254.169.254', '127.0.0.1', '100.100.100.100', '192.168.1.1'):
            self.assertFalse(any(ipaddress.ip_address(address) in n for n in nets), address)
        for address in ('140.82.112.3', '5.135.138.34', '1.1.1.1'):
            self.assertTrue(any(ipaddress.ip_address(address) in n for n in nets), address)

    def test_an_acl_without_the_host_excluded_is_refused(self):
        with self.assertRaises(pool.PoolError):
            pool.acl([])


class ConfigTest(unittest.TestCase):
    def test_unknown_keys_and_a_bad_repository_are_refused(self):
        with tempfile.NamedTemporaryFile('w', suffix='.toml', delete=False) as file:
            file.write('idle_targets = 3\n')
        self.addCleanup(Path(file.name).unlink)
        with self.assertRaises(pool.PoolError):
            pool.load_config(file.name)
        Path(file.name).write_text('repository = "a/b/c"\n')
        with self.assertRaises(pool.PoolError):
            pool.load_config(file.name)

    def test_the_example_config_is_the_defaults(self):
        self.assertEqual(pool.load_config(HERE / 'pool.example.toml'), pool.DEFAULTS)


if __name__ == '__main__':
    unittest.main()
