"""Reviewed local commands cannot inherit worker fan-out execution details."""
import unittest
import tomllib

from pandora.client import placement
from pandora.errors import Refused, ConfigError
from pandora.tests.test_placement import PlacementCase, CONFIG, RecordingWorker
from pandora.tests.test_fallback import FakeWorker
from pandora.errors import WorkerUnreachable
from pandora.config import classify, loader


class LocalMapping(unittest.TestCase):
    def config(self, local=''):
        return loader.validate(tomllib.loads('''version = 1
[repo]
name = "demo"
entrypoints = ["pnpm"]
[[jobs]]
id = "surface"
args = "optional"
forms = [{ prefix = ["surface"] }]
run = { argv = ["node", "runner.mjs", "run", "{args}", "--no-build"], env = { REMOTE = "1" } }
''' + local + '''
[jobs.shards]
strategy = "argv"
template = "--shard={i}/{n}"
default = 2
[worker]
base_image = "images:ubuntu/26.04"
'''))

    def test_mapped_local_uses_complete_command(self):
        config = self.config('local = { argv = ["node", "runner.mjs", "run", "{args}"], env = { LOCAL = "1" }, cwd = "scripts", unset = ["REMOTE"] }\n')
        verdict = classify.classify(config, ['pnpm', 'surface', 'desk', '--grep', 'case'])
        job = config['jobs']['surface']
        decision = placement.decide(job, verdict['plan'], 'local')
        plan = decision['plan']
        self.assertEqual(plan['argv'], ['node', 'runner.mjs', 'run', 'desk', '--grep', 'case'])
        self.assertIsNone(plan['shards'])
        self.assertEqual(plan['env'], {'LOCAL': '1'})
        self.assertEqual(plan['cwd'], 'scripts')
        self.assertEqual(plan['env_unset'], ['REMOTE'])
        self.assertEqual(placement.local_plan(verdict['plan']), plan)

    def test_explicit_update_is_in_place_but_automatic_fallback_refuses(self):
        config = self.config('local = { argv = ["node", "runner.mjs", "run", "{args}"] }\n')
        verdict = classify.classify(config, ['pnpm', 'surface', 'desk'])
        remote = dict(verdict['plan'], writeback=True, options={'update': True},
                      outputs=[{'kind': 'writeback', 'paths': ['fixtures']},
                               {'kind': 'artifacts', 'paths': ['reports']}])
        remote['local_run'] = dict(remote['local_run'], argv=['node', 'runner.mjs', 'run', 'desk', '--update'])
        local = placement.decide(config['jobs']['surface'], remote, 'local')['plan']
        self.assertEqual(local['argv'][-1], '--update')
        self.assertFalse(local['writeback'])
        self.assertEqual(local['outputs'], [{'kind': 'artifacts', 'paths': ['reports']}])
        from pandora.client import fallback
        self.assertEqual(fallback.decide(cause='worker-down', writeback=True,
                                        declared={'action': 'local', 'on': ['worker-down']})['action'], 'refuse')

    def test_invalid_local_commands_are_refused_at_load(self):
        with self.assertRaises(ConfigError):
            self.config('where = "local"\nlocal = { argv = ["node", "runner.mjs", "{args}"] }\n')
        with self.assertRaises(ConfigError):
            self.config('local = { argv = ["node", "runner.mjs"] }\n')
        with self.assertRaises(ConfigError):
            self.config('local = { argv = ["node", "runner.mjs", "{args}", "--shard={i}/{n}"] }\n')

    def test_unmapped_shards_fail_closed(self):
        config = self.config()
        verdict = classify.classify(config, ['pnpm', 'surface', 'desk'])
        with self.assertRaises(Refused):
            placement.decide(config['jobs']['surface'], verdict['plan'], 'local')


class MappedDaemon(PlacementCase):
    def setUp(self):
        super().setUp()
        local = 'local = { argv = ["sh", "-c", "echo $LOCAL $* >> %(marker)s", "--", "{args}"], env = { LOCAL = "mapped" } }\n'
        text = CONFIG % {'marker': self.marker}
        text = text.replace('[jobs.shards]', (local % {'marker': self.marker}) + '[jobs.shards]')
        (self.repo / 'pandora.toml').write_text(text)

    def test_explicit_local_never_submits_worker_plan(self):
        answer = self.place(['pnpm', 'surface', 'desk'], 'local')
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertEqual(self.marker.read_text().splitlines(), ['mapped desk'])
        self.assertEqual(RecordingWorker.plans, [])

    def test_worker_unreachable_uses_mapped_local_command_once(self):
        FakeWorker.raises = WorkerUnreachable('no worker')
        answer = self.call(['pnpm', 'surface', 'desk'])
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertEqual(self.marker.read_text().splitlines(), ['mapped desk'])
        self.assertEqual(answer.accepted['lane'], 'local')

    def test_explicit_remote_with_mapping_never_falls_back(self):
        FakeWorker.raises = WorkerUnreachable('no worker')
        answer = self.place(['pnpm', 'surface', 'desk'], 'remote')
        self.assertEqual(answer.exit, 70, answer.error)
        self.assertFalse(self.marker.exists())
        self.assertIsNone(answer.accepted)
