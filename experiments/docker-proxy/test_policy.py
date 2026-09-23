import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
from policy import Denied, Policy, decide, inject_filter, strip_version  # noqa: E402

RUN = 'run-x'
CLIENT = '/Users/dev/Code/eichler-wt/feature'
WORKER = '/srv/pandora/runs/run-x/source'


def policy(**kwargs):
    return Policy(RUN, kwargs.pop('run_dir', WORKER), client_root=kwargs.pop('client_root', CLIENT),
                  docker_socket='/var/run/docker.sock', **kwargs)


class VersionTests(unittest.TestCase):
    def test_a_negotiated_version_prefix_is_stripped_before_routing(self):
        self.assertEqual(strip_version('/v1.51/containers/json'), '/containers/json')
        self.assertEqual(strip_version('/v1/containers/json'), '/containers/json')
        self.assertEqual(strip_version('/containers/json'), '/containers/json')

    def test_only_the_leading_prefix_goes(self):
        self.assertEqual(strip_version('/v1.51/volumes/v1.2'), '/volumes/v1.2')

    def test_a_prefixed_create_still_routes_to_the_creator(self):
        self.assertEqual(decide('POST', '/v1.51/containers/create?name=a'),
                         {'action': 'create', 'kind': 'container', 'capture': 'container'})


class RouteTests(unittest.TestCase):
    def test_the_calls_docker_compose_makes_are_all_classified(self):
        cases = {
            ('GET', '/_ping'): 'allow',
            ('GET', '/version'): 'allow',
            ('GET', '/containers/json?filters=%7B%7D'): 'list',
            ('GET', '/networks'): 'list',
            ('GET', '/volumes'): 'list',
            ('GET', '/events?since=1'): 'list',
            ('POST', '/containers/create'): 'create',
            ('POST', '/networks/create'): 'create',
            ('POST', '/volumes/create'): 'create',
            ('POST', '/containers/abc/start'): 'scoped',
            ('POST', '/containers/abc/wait'): 'scoped',
            ('GET', '/containers/abc/logs?follow=1'): 'scoped',
            ('POST', '/containers/abc/attach?stream=1'): 'scoped',
            ('POST', '/containers/abc/exec'): 'scoped',
            ('POST', '/exec/def/start'): 'scoped',
            ('DELETE', '/containers/abc'): 'scoped',
            ('POST', '/networks/net/connect'): 'scoped',
            ('GET', '/images/postgres:16/json'): 'allow',
            ('POST', '/images/create?fromImage=postgres&tag=16'): 'allow',
        }
        for (method, target), expected in cases.items():
            with self.subTest(target=target):
                self.assertEqual(decide(method, target)['action'], expected)

    def test_build_and_the_shared_image_cache_are_refused_with_a_reason(self):
        for method, target in (('POST', '/build'), ('POST', '/session'),
                               ('DELETE', '/images/postgres:16'),
                               ('POST', '/images/prune'), ('POST', '/commit'),
                               ('POST', '/containers/prune'), ('POST', '/volumes/prune'),
                               ('POST', '/networks/prune'), ('GET', '/system/df'),
                               ('GET', '/swarm'), ('POST', '/images/x/push')):
            with self.subTest(target=target):
                decision = decide(method, target)
                self.assertEqual(decision['action'], 'deny')
                self.assertIn('pandora-proxy', decision['message'])

    def test_a_network_connect_carries_the_container_field_to_check(self):
        self.assertEqual(decide('POST', '/networks/n/connect').get('body_container'), 'Container')


class FilterTests(unittest.TestCase):
    def test_an_empty_query_gains_the_run_label(self):
        parsed = parse_qs(inject_filter('', RUN))
        self.assertEqual(json.loads(parsed['filters'][0]), {'label': [f'pandora.run={RUN}']})

    def test_the_callers_own_filters_survive(self):
        existing = json.dumps({'label': ['com.docker.compose.project=p'], 'status': ['running']})
        query = inject_filter(f'all=1&filters={_q(existing)}', RUN)
        parsed = parse_qs(query)
        self.assertEqual(parsed['all'], ['1'])
        filters = json.loads(parsed['filters'][0])
        self.assertEqual(filters['status'], ['running'])
        self.assertEqual(filters['label'],
                         ['com.docker.compose.project=p', f'pandora.run={RUN}'])

    def test_the_legacy_map_form_of_a_label_filter_is_normalised(self):
        existing = json.dumps({'label': {'a=b': True, 'c=d': False}})
        filters = json.loads(parse_qs(inject_filter(f'filters={_q(existing)}', RUN))['filters'][0])
        self.assertEqual(filters['label'], ['a=b', f'pandora.run={RUN}'])

    def test_every_key_is_normalised_because_the_daemon_refuses_a_mixed_set(self):
        # What `docker compose up` sends on its first network lookup. Leaving
        # `name` as a map and adding `label` as a list is `invalid filter`.
        existing = json.dumps({'name': {'p_default': True}})
        filters = json.loads(parse_qs(inject_filter(f'filters={_q(existing)}', RUN))['filters'][0])
        self.assertEqual(filters, {'name': ['p_default'], 'label': [f'pandora.run={RUN}']})

    def test_the_label_is_added_once(self):
        first = inject_filter('', RUN)
        second = inject_filter(first, RUN)
        self.assertEqual(json.loads(parse_qs(second)['filters'][0])['label'],
                         [f'pandora.run={RUN}'])


def _q(value):
    from urllib.parse import quote
    return quote(value, safe='')


class RewriteTests(unittest.TestCase):
    def test_the_client_worktree_maps_onto_the_run_directory(self):
        self.assertEqual(policy().rewrite_source(CLIENT), WORKER)
        self.assertEqual(policy().rewrite_source(CLIENT + '/tools/stack/init.sql'),
                         WORKER + '/tools/stack/init.sql')

    def test_the_rewrite_is_the_identity_when_the_two_roots_agree(self):
        same = policy(client_root=CLIENT, run_dir=CLIENT)
        self.assertEqual(same.rewrite_source(CLIENT + '/a'), CLIENT + '/a')

    def test_a_prefix_that_is_not_a_path_boundary_does_not_match(self):
        with self.assertRaises(Denied):
            policy().rewrite_source(CLIENT + '-other/secret')

    def test_a_relative_traversal_is_normalised_before_the_containment_check(self):
        with self.assertRaises(Denied) as caught:
            policy().rewrite_source(CLIENT + '/../../../etc/shadow')
        self.assertIn('outside this run', caught.exception.message)

    def test_a_bind_keeps_its_destination_and_options(self):
        self.assertEqual(policy().rewrite_bind(f'{CLIENT}/a:/w/a:ro'), f'{WORKER}/a:/w/a:ro')

    def test_a_named_volume_source_is_left_alone(self):
        self.assertEqual(policy().rewrite_bind('postgres-data:/var/lib/postgresql/data'),
                         'postgres-data:/var/lib/postgresql/data')

    def test_the_paths_that_are_never_acceptable(self):
        for source in ('/', '/var/run/docker.sock', '/run/docker.sock',
                       '/Users/dev/.docker/run/docker.sock', '/proc', '/sys/fs/cgroup',
                       '/etc/passwd', '/Users/dev/.ssh'):
            with self.subTest(source=source):
                with self.assertRaises(Denied):
                    policy().rewrite_source(source)

    def test_an_extra_read_path_may_be_allowed_explicitly(self):
        relaxed = policy(extra_read_paths=('/opt/pandora/cache',))
        self.assertEqual(relaxed.rewrite_source('/opt/pandora/cache/pnpm'),
                         '/opt/pandora/cache/pnpm')


class ContainerCreateTests(unittest.TestCase):
    def test_the_run_label_the_cgroup_and_the_default_caps_are_injected(self):
        subject = policy(cgroup_parent='pandora-run-x.slice',
                         default_memory=512 * 1024 * 1024, default_nanocpus=1_500_000_000)
        body = subject.container_create({'Image': 'postgres:16', 'Labels': {'keep': 'me'}})
        self.assertEqual(body['Labels'], {'keep': 'me', 'pandora.run': RUN})
        self.assertEqual(body['HostConfig']['CgroupParent'], 'pandora-run-x.slice')
        self.assertEqual(body['HostConfig']['Memory'], 512 * 1024 * 1024)
        self.assertEqual(body['HostConfig']['NanoCpus'], 1_500_000_000)

    def test_a_request_that_states_its_own_caps_keeps_them(self):
        subject = policy(default_memory=512 * 1024 * 1024, default_nanocpus=1_500_000_000)
        body = subject.container_create({'HostConfig': {'Memory': 64, 'NanoCpus': 7}})
        self.assertEqual(body['HostConfig']['Memory'], 64)
        self.assertEqual(body['HostConfig']['NanoCpus'], 7)

    def test_a_create_without_a_host_config_still_gets_one(self):
        body = policy(cgroup_parent='s.slice').container_create({'Image': 'x'})
        self.assertEqual(body['HostConfig']['CgroupParent'], 's.slice')

    def test_binds_and_mounts_are_both_rewritten(self):
        body = policy().container_create({'HostConfig': {
            'Binds': [f'{CLIENT}/src:/src'],
            'Mounts': [{'Type': 'bind', 'Source': f'{CLIENT}/sql', 'Target': '/sql'},
                       {'Type': 'volume', 'Source': 'data', 'Target': '/var/lib'}]}})
        self.assertEqual(body['HostConfig']['Binds'], [f'{WORKER}/src:/src'])
        self.assertEqual(body['HostConfig']['Mounts'][0]['Source'], f'{WORKER}/sql')
        self.assertEqual(body['HostConfig']['Mounts'][1]['Source'], 'data')

    def test_the_escapes_are_refused_one_by_one(self):
        cases = [
            {'Privileged': True},
            {'NetworkMode': 'host'},
            {'NetworkMode': 'container:abc'},
            {'PidMode': 'host'},
            {'IpcMode': 'host'},
            {'UTSMode': 'host'},
            {'CgroupnsMode': 'host'},
            {'UsernsMode': 'host'},
            {'CapAdd': ['SYS_ADMIN']},
            {'Devices': [{'PathOnHost': '/dev/kvm'}]},
            {'DeviceRequests': [{'Driver': 'nvidia'}]},
            {'DeviceCgroupRules': ['c 1:1 rwm']},
            {'SecurityOpt': ['seccomp=unconfined']},
            {'VolumesFrom': ['other']},
            {'Runtime': 'sysbox-runc'},
            {'Binds': ['/:/host']},
            {'Binds': ['/var/run/docker.sock:/var/run/docker.sock']},
            {'Mounts': [{'Type': 'bind', 'Source': '/', 'Target': '/host'}]},
            {'Mounts': [{'Type': 'cluster', 'Source': 'x', 'Target': '/x'}]},
        ]
        for host in cases:
            with self.subTest(host=host):
                with self.assertRaises(Denied) as caught:
                    policy().container_create({'HostConfig': dict(host)})
                self.assertTrue(caught.exception.message.startswith('pandora-proxy:'))

    def test_network_mode_none_and_a_compose_network_are_fine(self):
        for mode in ('none', 'ike-dev_default', 'bridge'):
            policy().container_create({'HostConfig': {'NetworkMode': mode}})


class OtherCreateTests(unittest.TestCase):
    def test_a_network_and_a_volume_both_get_the_run_label(self):
        self.assertEqual(policy().network_create({'Name': 'n'})['Labels'],
                         {'pandora.run': RUN})
        self.assertEqual(policy().volume_create({'Name': 'v'})['Labels'],
                         {'pandora.run': RUN})

    def test_a_local_volume_with_a_host_device_is_a_bind_in_disguise(self):
        with self.assertRaises(Denied):
            policy().volume_create({'Name': 'v', 'DriverOpts': {'type': 'none',
                                                                'device': '/etc',
                                                                'o': 'bind'}})

    def test_a_host_network_driver_is_refused(self):
        with self.assertRaises(Denied):
            policy().network_create({'Name': 'n', 'Driver': 'host'})


class OwnershipTests(unittest.TestCase):
    def test_only_this_run_s_label_counts(self):
        subject = policy()
        self.assertTrue(subject.owns({'pandora.run': RUN}))
        self.assertFalse(subject.owns({'pandora.run': 'other'}))
        self.assertFalse(subject.owns({}))
        self.assertFalse(subject.owns(None))


if __name__ == '__main__':
    unittest.main()
