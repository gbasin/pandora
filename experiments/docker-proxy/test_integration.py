"""The proxy against the real daemon. Skipped unless ``PANDORA_DOCKER_IT=1``.

Everything these tests create carries ``pandora.run=it-<pid>-<n>`` and a name
built from the same string, and every test sweeps that label whether it passed
or not, so a shared machine keeps whatever else is running on it.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import Engine  # noqa: E402
from policy import Policy  # noqa: E402
from proxy import RunProxy, sweep, usage  # noqa: E402

DOCKER = os.environ.get('PANDORA_DOCKER_SOCKET',
                        os.path.expanduser('~/.docker/run/docker.sock'))
# Unix socket paths are capped near 104 bytes on macOS, which rules out a
# temporary directory under /var/folders for the listener itself.
ROOT = '/tmp/pandora-dp-it'
IMAGE = 'busybox:latest'


def enabled():
    return os.environ.get('PANDORA_DOCKER_IT') == '1'


@unittest.skipUnless(enabled(), 'set PANDORA_DOCKER_IT=1 to run against the local daemon')
class IntegrationCase(unittest.TestCase):
    counter = 0

    def setUp(self):
        IntegrationCase.counter += 1
        self.run_id = f'it-{os.getpid()}-{IntegrationCase.counter}'
        self.engine = Engine(DOCKER)
        self.work = tempfile.mkdtemp(prefix='pandora-dp-work-')
        self.addCleanup(lambda: __import__('shutil').rmtree(self.work, ignore_errors=True))
        self.proxies = []
        self.addCleanup(self.cleanup)
        self.socket = self.start(self.run_id)

    def start(self, run_id, **kwargs):
        policy = Policy(run_id, kwargs.pop('run_dir', self.work), docker_socket=DOCKER, **kwargs)
        directory = os.path.join(ROOT, run_id)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = os.path.join(directory, 'docker.sock')
        proxy = RunProxy(policy, DOCKER, path)
        threading.Thread(target=proxy.serve, daemon=True).start()
        self.assertTrue(proxy.ready.wait(10), 'the proxy never started listening')
        self.proxies.append((run_id, proxy))
        return path

    def cleanup(self):
        for run_id, proxy in self.proxies:
            proxy.stop()
            sweep(DOCKER, run_id)

    def docker(self, *args, socket=None, check=True, stdin=None, timeout=180):
        environment = {**os.environ, 'DOCKER_HOST': f'unix://{socket or self.socket}'}
        environment.pop('DOCKER_CONTEXT', None)
        result = subprocess.run(['docker', *args], env=environment, capture_output=True,
                                text=True, input=stdin, timeout=timeout)
        if check and result.returncode != 0:
            self.fail(f'docker {" ".join(args)} exited {result.returncode}\n'
                      f'{result.stdout}\n{result.stderr}')
        return result

    def inspect(self, name):
        return self.engine.json('GET', f'/containers/{name}/json')


class PlainRunTests(IntegrationCase):
    def test_a_container_started_through_the_proxy_carries_the_run_label(self):
        name = f'{self.run_id}-sleep'
        self.docker('run', '-d', '--name', name, IMAGE, 'sleep', '60')
        document = self.inspect(name)
        self.assertEqual(document['Config']['Labels']['pandora.run'], self.run_id)

    def test_an_interactive_run_carries_stdin_and_stdout_through_the_hijack(self):
        result = self.docker('run', '-i', '--rm', IMAGE, 'cat', stdin='hello proxy\n')
        self.assertEqual(result.stdout.strip(), 'hello proxy')

    def test_a_bind_of_the_host_root_is_refused_with_a_readable_message(self):
        result = self.docker('run', '--rm', '-v', '/:/host', IMAGE, 'true', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pandora-proxy', result.stderr)
        self.assertIn('host root', result.stderr)

    def test_a_bind_of_the_docker_socket_is_refused(self):
        result = self.docker('run', '--rm', '-v', f'{DOCKER}:/var/run/docker.sock',
                             IMAGE, 'true', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Docker socket', result.stderr)

    def test_host_networking_and_privileged_are_refused(self):
        for flag in (('--network', 'host'), ('--privileged',), ('--pid', 'host'),
                     ('--cap-add', 'SYS_ADMIN')):
            with self.subTest(flag=flag):
                result = self.docker('run', '--rm', *flag, IMAGE, 'true', check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('pandora-proxy', result.stderr)

    def test_a_bind_outside_the_run_directory_is_refused_and_one_inside_is_not(self):
        inside = os.path.join(self.work, 'data')
        os.makedirs(inside, exist_ok=True)
        Path(inside, 'file').write_text('ok\n')
        result = self.docker('run', '--rm', '-v', f'{inside}:/data:ro', IMAGE, 'cat', '/data/file')
        self.assertEqual(result.stdout.strip(), 'ok')
        outside = self.docker('run', '--rm', '-v', f'{os.path.dirname(self.work)}:/o',
                              IMAGE, 'true', check=False)
        self.assertNotEqual(outside.returncode, 0)
        self.assertIn('outside this run', outside.stderr)

    def test_a_build_is_refused_because_the_worker_owns_image_production(self):
        Path(self.work, 'Dockerfile').write_text(f'FROM {IMAGE}\nRUN true\n')
        result = self.docker('build', self.work, check=False, timeout=120)
        self.assertNotEqual(result.returncode, 0)


class CgroupTests(IntegrationCase):
    """Whether the cgroup slice the proxy asks for is honoured, or quietly ignored."""

    def setUp(self):
        super().setUp()
        self.slice = f'pandora-{self.run_id}.slice'
        self.socket = self.start(f'{self.run_id}-cg', cgroup_parent=self.slice,
                                 run_dir=self.work)
        self.run_id = f'{self.run_id}-cg'

    def test_the_requested_cgroup_parent_is_recorded_and_reported(self):
        name = f'{self.run_id}-sleep'
        result = self.docker('run', '-d', '--name', name, IMAGE, 'sleep', '30', check=False)
        observed = {'requested': self.slice, 'accepted': result.returncode == 0}
        if result.returncode == 0:
            document = self.inspect(name)
            observed['reported'] = document['HostConfig']['CgroupParent']
        else:
            observed['error'] = result.stderr.strip()[:300]
        print('\ncgroup-parent: ' + json.dumps(observed))
        self.assertIn('accepted', observed)


class ScopeTests(IntegrationCase):
    def test_a_second_run_sees_neither_the_first_run_s_containers_nor_the_users(self):
        name = f'{self.run_id}-hidden'
        self.docker('run', '-d', '--name', name, IMAGE, 'sleep', '60')
        mine = self.docker('ps', '-a', '--format', '{{.Names}}').stdout.split()
        self.assertEqual(mine, [name])

        other_socket = self.start(f'{self.run_id}-b', run_dir=self.work)
        theirs = self.docker('ps', '-a', '--format', '{{.Names}}', socket=other_socket)
        self.assertEqual(theirs.stdout.strip(), '')

        blocked = self.docker('stop', name, socket=other_socket, check=False)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('no such container for this run', blocked.stderr)
        self.assertEqual(self.inspect(name)['State']['Running'], True)

        # The user's own containers are equally invisible: nothing on this
        # machine outside the run carries the label.
        everything = self.engine.json('GET', '/containers/json?all=1')
        self.assertGreater(len(everything), 1)


class ComposeTests(IntegrationCase):
    COMPOSE = """
services:
  first:
    image: busybox:latest
    command: ['sh', '-c', 'echo first up; sleep 120']
    volumes: ['state:/state']
  second:
    image: busybox:latest
    command: ['sh', '-c', 'echo second up; sleep 120']
    depends_on: ['first']
volumes:
  state:
"""

    def project(self):
        path = os.path.join(self.work, 'compose.yml')
        Path(path).write_text(self.COMPOSE)
        return ['compose', '-p', self.run_id, '-f', path]

    def test_a_two_service_project_comes_up_labelled_and_goes_down_clean(self):
        compose = self.project()
        self.docker(*compose, 'up', '-d', timeout=300)
        names = self.docker(*compose, 'ps', '--format', '{{.Name}}').stdout.split()
        self.assertEqual(len(names), 2)
        for name in names:
            self.assertEqual(self.inspect(name)['Config']['Labels']['pandora.run'], self.run_id)
        networks = self.engine.json(
            'GET', '/networks?filters=' + _filters(self.run_id))
        self.assertTrue(any(item['Name'] == f'{self.run_id}_default' for item in networks))
        volumes = self.engine.json('GET', '/volumes?filters=' + _filters(self.run_id))
        self.assertTrue(any(item['Name'] == f'{self.run_id}_state'
                            for item in volumes['Volumes'] or []))

        logs = self.docker(*compose, 'logs', '--no-color')
        self.assertIn('first up', logs.stdout)

        execed = self.docker(*compose, 'exec', '-T', 'first', 'echo', 'exec ok')
        self.assertIn('exec ok', execed.stdout)

        live = usage(DOCKER, self.run_id)
        self.assertEqual(live['totals']['containers'], 2)
        self.assertGreater(live['totals']['memory_mib'], 0)
        print('\ncompose-usage: ' + json.dumps(live['totals']))

        self.docker(*compose, 'down', '-v', timeout=180)
        receipt = sweep(DOCKER, self.run_id)
        self.assertTrue(receipt['clean'], receipt)
        self.assertEqual(receipt['counts'], {'containers': 0, 'networks': 0, 'volumes': 0})

    def test_a_killed_project_is_swept_by_the_label_alone(self):
        compose = self.project()
        self.docker(*compose, 'up', '-d', timeout=300)
        before = sweep(DOCKER, self.run_id)
        self.assertEqual(before['counts']['containers'], 2)
        self.assertEqual(before['counts']['networks'], 1)
        self.assertEqual(before['counts']['volumes'], 1)
        self.assertTrue(before['clean'], before)
        self.assertEqual(before['remaining'],
                         {'containers': [], 'networks': [], 'volumes': []})


class UsageTests(IntegrationCase):
    def test_usage_reports_a_live_number_per_container(self):
        name = f'{self.run_id}-busy'
        self.docker('run', '-d', '--name', name, '--memory', '64m', IMAGE,
                    'sh', '-c', 'while true; do :; done')
        time.sleep(2)
        report = usage(DOCKER, self.run_id)
        self.assertEqual(report['totals']['containers'], 1)
        container = report['containers'][0]
        self.assertGreater(container['memory_bytes'], 0)
        self.assertEqual(container['memory_limit_mib'], 64.0)
        print('\nusage: ' + json.dumps(report['containers']))


def _filters(run_id):
    from urllib.parse import quote
    return quote(json.dumps({'label': [f'pandora.run={run_id}']}), safe='')


if __name__ == '__main__':
    unittest.main()
