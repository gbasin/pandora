"""The caller's environment: what reaches a routed run, and what refuses one.

`[env] passthrough` names the caller's variables a run gets. Before this the plan
shipped only the configuration's own `env`, so `TZ` in `passthrough` changed
nothing on the worker. And `reject_if_set` was checked against the environment
after the secret and platform filters, so `NODE_OPTIONS` or any `*_TOKEN` could
never trigger it.
"""
import os
import time
import unittest
from unittest import mock

from pandora.client import envfilter, shim
from pandora.errors import Refused
from pandora.tests.test_fallback import DaemonCase

CONFIG = '''
version = 1
[repo]
name = "demo"
entrypoints = ["pnpm"]
[env]
set = { CI = "1", FORCED = "config" }
passthrough = ["TZ", "FORCED", "GONE", "NODE_OPTIONS", "NPM_TOKEN"]
unset = ["GONE"]
reject_if_set = ["NODE_OPTIONS"]
[[jobs]]
id = "unit"
size = "small"
args = "optional"
reject_if_set = ["NPM_TOKEN"]
forms = [{ prefix = ["unit"] }]
run = { argv = ["true", "{args}"] }
[worker]
base_image = "images:ubuntu/26.04"
'''


class Passthrough(DaemonCase):
    def setUp(self):
        super().setUp()
        (self.repo / 'pandora.toml').write_text(CONFIG)
        # The daemon reloads on a new mtime; make sure this one is new.
        stamp = time.time() + 5
        os.utime(self.repo / 'pandora.toml', (stamp, stamp))

    def plan(self, env, present=None):
        request = {'cwd': str(self.repo), 'argv': ['pnpm', 'unit'], 'env': env}
        if present is not None:
            request['env_present'] = present
        try:
            _repo, _config, verdict = self.daemon.plan_for(request)
        except Refused as error:
            return {'decision': 'reject', 'message': str(error)}
        return verdict

    def test_a_passthrough_name_reaches_the_plan_under_the_config_env(self):
        env = self.plan({'TZ': 'Europe/Paris', 'FORCED': 'caller', 'GONE': 'x',
                         'OTHER': 'not declared'})['plan']['env']
        self.assertEqual(env['TZ'], 'Europe/Paris')
        self.assertEqual(env['FORCED'], 'config', 'the configuration outranks the caller')
        self.assertEqual(env['CI'], '1')
        self.assertNotIn('GONE', env, '`unset` applies to passed-through names too')
        self.assertNotIn('OTHER', env, 'only declared names travel')

    def test_reject_if_set_sees_names_the_filter_dropped(self):
        with mock.patch.dict(os.environ, {'NODE_OPTIONS': '--inspect', 'TZ': 'UTC'}):
            request = shim.build_request(['unit'], cwd=str(self.repo))
        self.assertNotIn('NODE_OPTIONS', request['env'], 'platform names still never travel')
        verdict = self.plan(request['env'], request['env_present'])
        self.assertEqual(verdict['decision'], 'reject')
        self.assertIn('NODE_OPTIONS', verdict['message'])

    def test_a_secret_shaped_job_rejection_triggers_without_shipping_the_value(self):
        with mock.patch.dict(os.environ, {'NPM_TOKEN': 'hunter2'}):
            request = shim.build_request(['unit'], cwd=str(self.repo))
        self.assertNotIn('NPM_TOKEN', request['env'])
        self.assertNotIn('hunter2', repr(request))
        verdict = self.plan(request['env'], request['env_present'])
        self.assertEqual(verdict['decision'], 'reject')
        self.assertIn('NPM_TOKEN', verdict['message'])

    def test_a_declared_name_the_filter_drops_is_named_on_stderr(self):
        lines = envfilter.notices(['TZ', 'NPM_TOKEN', 'LANG'],
                                  {'secret': ['NPM_TOKEN', 'AWS_SECRET_ACCESS_KEY'],
                                   'platform': ['LANG', 'PATH']})
        self.assertEqual(len(lines), 2)
        self.assertIn('NPM_TOKEN', lines[0])
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', lines[0], 'an undeclared name was never going')
        self.assertIn('LANG', lines[1])
        self.assertEqual(envfilter.notices(['TZ'], {'secret': ['X_TOKEN'], 'platform': []}), [])


if __name__ == '__main__':
    unittest.main()
