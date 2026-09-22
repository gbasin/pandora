"""The worker half, with no worker: manifests, drift, pins, GC policy, parsing."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from pandora.errors import ConfigError
from pandora.executor.interface import Instance, Receipt, Toolchain
from pandora.worker import gc, goldens, pins, provision, versions


class Versions(unittest.TestCase):
    def test_defaults_are_a_complete_manifest(self):
        manifest = versions.load(None)
        self.assertIn('incus', manifest['packages'])
        self.assertEqual(manifest['worker']['pool'], 'pandorapool')
        self.assertNotIn('docker.io', manifest['packages'],
                         'docker belongs inside a golden, not on the host')

    def test_unknown_table_is_refused(self):
        with self.assertRaises(ConfigError):
            versions.normalise({'nonsense': {}})

    def test_unknown_worker_key_is_refused(self):
        with self.assertRaises(ConfigError):
            versions.normalise({'worker': {'pooll': 'x'}})

    def test_device_must_be_a_dev_path(self):
        with self.assertRaises(ConfigError):
            versions.normalise({'worker': {'device': 'sdb'}})

    def test_digest_ignores_nothing_declared_and_changes_with_a_pin(self):
        one = versions.normalise({})
        two = versions.normalise({'packages': {'incus': '6.0.5-8'}})
        self.assertNotEqual(versions.digest(one), versions.digest(two))
        self.assertEqual(versions.digest(one), versions.digest(versions.normalise({})))

    def test_render_round_trips(self):
        manifest = versions.normalise({'packages': {'incus': '6.0.5-8'},
                                       'worker': {'loop_size_gib': 24}})
        import tomllib
        again = versions.normalise(tomllib.loads(versions.render(manifest)))
        self.assertEqual(versions.digest(manifest), versions.digest(again))


class Drift(unittest.TestCase):
    def setUp(self):
        self.manifest = versions.normalise({'packages': {'incus': '6.0.5-8', 'git': '*'}})

    def test_missing_package_drifts(self):
        items = versions.drift(self.manifest, {'packages': {'incus': '6.0.5-8'}})
        self.assertTrue(any(item['name'] == 'git' and item['have'] is None for item in items))

    def test_star_tolerates_any_version(self):
        items = versions.drift(self.manifest,
                               {'packages': dict.fromkeys(self.manifest['packages'], '9')})
        self.assertEqual([item['name'] for item in items], ['incus'])

    def test_pin_does_not_tolerate_a_near_miss(self):
        have = dict.fromkeys(self.manifest['packages'], '1')
        have['incus'] = '6.0.5-9'
        items = versions.drift(self.manifest, {'packages': have})
        self.assertEqual([item['name'] for item in items], ['incus'])

    def test_a_missing_object_is_reported_as_an_object(self):
        have = dict.fromkeys(self.manifest['packages'], '1')
        have['incus'] = '6.0.5-8'
        items = versions.drift(self.manifest,
                               {'packages': have, 'missing': {'pool pandorapool': 'gone'}})
        self.assertEqual([(item['kind'], item['name']) for item in items],
                         [('object', 'pool pandorapool')])


class Pinning(unittest.TestCase):
    def test_an_unpinned_toolchain_keeps_its_old_fingerprint(self):
        # The two goldens on the live worker were built before pinning existed.
        # If adding the field changed their names they would all be orphaned.
        plain = Toolchain(base_image='images:ubuntu/26.04', packages=('git',))
        self.assertEqual(plain.fingerprint(),
                         Toolchain(base_image='images:ubuntu/26.04',
                                   packages=('git',), pins=()).fingerprint())
        self.assertFalse(plain.pinned)

    def test_a_pin_changes_the_fingerprint(self):
        plain = Toolchain(packages=('git',))
        pinned = Toolchain(packages=('git',), pins=(('base_image', 'abc'),))
        self.assertNotEqual(plain.fingerprint(), pinned.fingerprint())
        self.assertTrue(pinned.pinned)

    def test_every_pinned_input_moves_the_fingerprint(self):
        base = Toolchain(packages=('git',), service_images=('postgres:16',))
        seen = set()
        for extra in (('base_image', 'a'), ('lockfile:pnpm-lock.yaml', 'sha256:b'),
                      ('service:postgres:16', 'sha256:c')):
            seen.add(Toolchain(packages=base.packages, service_images=base.service_images,
                               pins=(extra,)).fingerprint())
        self.assertEqual(len(seen), 3)

    def test_pin_order_does_not_matter_to_the_caller(self):
        one = Toolchain(pins=tuple(sorted((('b', '2'), ('a', '1')))))
        two = Toolchain(pins=(('a', '1'), ('b', '2')))
        self.assertEqual(one.fingerprint(), two.fingerprint())

    def test_split_image_understands_the_three_shapes(self):
        self.assertEqual(pins.split_image('postgres:16'),
                         ('registry-1.docker.io', 'library/postgres', '16'))
        self.assertEqual(pins.split_image('edoburu/pgbouncer:latest'),
                         ('registry-1.docker.io', 'edoburu/pgbouncer', 'latest'))
        self.assertEqual(pins.split_image('ghcr.io/neondatabase/wsproxy:latest'),
                         ('ghcr.io', 'neondatabase/wsproxy', 'latest'))

    def test_lockfile_pins_hash_the_root_only(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'pnpm-lock.yaml').write_text('lock\n')
            (Path(root) / 'nested').mkdir()
            (Path(root) / 'nested' / 'pnpm-lock.yaml').write_text('other\n')
            found = pins.lockfile_pins(root)
        self.assertEqual(list(found), ['lockfile:pnpm-lock.yaml'])
        self.assertTrue(found['lockfile:pnpm-lock.yaml'].startswith('sha256:'))

    def test_a_bearer_challenge_without_a_realm_is_refused(self):
        with self.assertRaises(pins.PinFailed):
            pins.bearer('Bearer service="x"')
        with self.assertRaises(pins.PinFailed):
            pins.bearer('Basic realm="x"')


class ProvisionParsing(unittest.TestCase):
    def test_steps_and_facts_are_separated(self):
        steps, found = provision.parse(
            'STEP\tpresent\tpool\tpandorapool\n'
            'noise that is not a step\n'
            'STEP\tchanged\tbridge\tpandorabr0 10.141.0.1/24\n'
            'FACT\tpackage.incus\t6.0.5-8\n')
        self.assertEqual([item['state'] for item in steps], ['present', 'changed'])
        self.assertEqual(found['package.incus'], '6.0.5-8')

    def test_the_preamble_quotes_a_multiline_manifest(self):
        manifest = versions.load(None)
        text = provision.preamble(manifest, root='/w', engine_root='/e',
                                  pool_file='/w/pool.img')
        self.assertIn('\nROOT=/w\n', text)
        self.assertIn('MANIFEST=', text)
        self.assertIn('[packages]', text)
        # One assignment per line before the manifest's own newlines: a shell
        # that mis-parses this writes a pool onto the wrong device.
        self.assertTrue(text.startswith('BRIDGE='), text[:40])
        self.assertTrue(text.endswith("\n"), text[-20:])

    def test_a_pinned_package_reaches_the_script_as_name_equals_version(self):
        manifest = versions.normalise({'packages': {'incus': '6.0.5-8'}})
        text = provision.preamble(manifest, root='/w', engine_root='/e', pool_file='/p')
        self.assertIn('incus=6.0.5-8', text)


class FakeDriver:
    """Enough of IncusDriver for the sweeps, and nothing that touches a host."""

    def __init__(self, instances, project='pandora', pool='pandorapool'):
        self.project, self.pool = project, pool
        self._instances = list(instances)
        self.destroyed = []

    def instances(self):
        return [dict(item) for item in self._instances]

    def qgroups(self):
        return {'containers/%s_%s' % (self.project, item['name']): (4 << 30, 1 << 20)
                for item in self._instances}

    def incus(self, *args, **kwargs):
        return 1, '', 'no volumes here'

    def pool_usage(self):
        return {'ok': True, 'pool': self.pool, 'free_gib': 9.0, 'total_bytes': 18 << 30,
                'used_bytes': 9 << 30, 'used_fraction': 0.5}

    def destroy(self, instance):
        self.destroyed.append(instance.name)
        self._instances = [item for item in self._instances if item['name'] != instance.name]
        return Receipt(run_id=instance.run_id, instance=instance.name, seconds=0.5,
                       instance_gone=True, volume_gone=True, veth_gone=True, cgroup_gone=True)


class Sweeps(unittest.TestCase):
    """GC against a real ledger and real attempt directories, with a fake host."""

    def setUp(self):
        from pandora.engine.ledger import Ledger
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'runs').mkdir()
        # The real ledger, not a stand-in: the sweeps read it with their own
        # query and a hand-rolled table would hide a column that moved.
        self.ledger = Ledger(self.root / 'ledger.db')

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def attempt(self, run_id, repo, state, spec, at):
        directory = self.root / 'runs' / run_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'toolchain.json').write_text(json.dumps(spec))
        self.ledger.claim('req-' + run_id, run_id, repo=repo, job='journey',
                          input_id='i' + run_id, source_path='/src', argv=['node'],
                          env={}, cwd='/work', outputs=[], size_class='large')
        self.ledger.update(run_id, state=state, instance='run-' + run_id)
        self.ledger.db.execute('UPDATE attempts SET created=? WHERE run_id=?', (at, run_id))

    @staticmethod
    def spec(source_id, **extra):
        return {'base_image': 'images:ubuntu/26.04', 'packages': ['git'],
                'node_version': '24.9.0', 'pnpm_version': '12.3.4',
                'service_images': [], 'install_command': 'pnpm i',
                'source_id': source_id, 'env': {}, **extra}

    def goldens_for(self, *source_ids):
        from pandora.engine.runner import toolchain_of
        return ['golden-' + toolchain_of(self.spec(sid)).fingerprint() for sid in source_ids]

    def test_the_index_names_the_repo_and_the_last_use(self):
        names = self.goldens_for('a', 'b')
        self.attempt('r1', 'eichler', 'finished', self.spec('a'), 100.0)
        self.attempt('r2', 'eichler', 'finished', self.spec('b'), 200.0)
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names])
        from pandora.engine.runner import Paths
        rows = goldens.index(Paths(self.root), driver)
        self.assertEqual([row['name'] for row in rows], [names[1], names[0]])
        self.assertEqual(rows[0]['repo'], 'eichler')
        self.assertEqual(rows[0]['last_used'], 200.0)
        self.assertFalse(rows[0]['pinned'])

    def test_a_golden_no_attempt_explains_is_still_listed(self):
        driver = FakeDriver([{'name': 'golden-deadbeefdeadbeef', 'state': 'STOPPED',
                              'created': ''}])
        from pandora.engine.runner import Paths
        rows = goldens.index(Paths(self.root), driver)
        self.assertEqual(rows[0]['repo'], None)
        self.assertEqual(rows[0]['referenced_bytes'], 4 << 30)

    def test_gc_removes_a_leaked_run_and_keeps_the_live_one(self):
        self.attempt('live', 'eichler', 'running', self.spec('a'), time.time())
        driver = FakeDriver([{'name': 'run-live', 'state': 'RUNNING', 'created': ''},
                             {'name': 'run-leaked', 'state': 'RUNNING', 'created': ''}])
        receipt = gc.sweep(self.root, driver, keep=2)
        self.assertEqual(driver.destroyed, ['run-leaked'])
        self.assertTrue(receipt['ok'])
        self.assertEqual([item['name'] for item in receipt['removed']], ['run-leaked'])

    def test_gc_keeps_the_newest_goldens_per_repo(self):
        names = self.goldens_for('a', 'b', 'c')
        for index, (source, at) in enumerate((('a', 100.0), ('b', 200.0), ('c', 300.0))):
            self.attempt('r%d' % index, 'eichler', 'finished', self.spec(source), at)
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names])
        receipt = gc.sweep(self.root, driver, keep=2)
        self.assertEqual(driver.destroyed, [names[0]], 'the least recently used goes')
        self.assertEqual({item['name'] for item in receipt['kept']}, {names[1], names[2]})

    def test_gc_never_removes_a_golden_a_live_attempt_needs(self):
        names = self.goldens_for('a', 'b', 'c')
        self.attempt('r0', 'eichler', 'running', self.spec('a'), 100.0)
        self.attempt('r1', 'eichler', 'finished', self.spec('b'), 200.0)
        self.attempt('r2', 'eichler', 'finished', self.spec('c'), 300.0)
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names])
        receipt = gc.sweep(self.root, driver, keep=1)
        self.assertEqual(driver.destroyed, [names[1]])
        whys = {item['name']: item['why'] for item in receipt['kept']}
        self.assertIn('live attempt', whys[names[0]])

    def test_dry_run_removes_nothing_and_still_says_what_it_would(self):
        names = self.goldens_for('a', 'b', 'c')
        for index, (source, at) in enumerate((('a', 100.0), ('b', 200.0), ('c', 300.0))):
            self.attempt('r%d' % index, 'eichler', 'finished', self.spec(source), at)
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names] +
                            [{'name': 'run-leaked', 'state': 'RUNNING', 'created': ''}])
        receipt = gc.sweep(self.root, driver, keep=2, dry_run=True)
        self.assertEqual(driver.destroyed, [])
        self.assertTrue(receipt['dry_run'])
        self.assertEqual({item['name'] for item in receipt['removed']},
                         {'run-leaked', names[0]})

    def test_a_pinned_attempt_names_a_different_golden(self):
        from pandora.engine.runner import toolchain_of
        plain = toolchain_of(self.spec('a'))
        pinned = toolchain_of(self.spec('a', pins={'base_image': 'abc123'}))
        self.assertNotEqual(plain.fingerprint(), pinned.fingerprint())
        self.assertTrue(pinned.pinned)

    def test_the_receipt_is_written_where_status_can_find_it(self):
        driver = FakeDriver([])
        receipt = gc.sweep(self.root, driver, keep=2)
        worker_dir = self.root / 'worker'
        gc.write_receipt(worker_dir, receipt)
        self.assertTrue(Path(receipt['receipt']).is_file())
        self.assertEqual(json.loads(Path(receipt['receipt']).read_text())['keep'], 2)


class Capacity(unittest.TestCase):
    """The engine's disk hook, against a driver whose pool is a dictionary."""

    class Pool:
        def __init__(self, usage):
            self.usage, self.pool = usage, 'pandorapool'

        def pool_usage(self):
            return self.usage

        capacity = None

    def driver(self, usage):
        from pandora.executor.incus import IncusDriver
        driver = IncusDriver.__new__(IncusDriver)
        driver.pool = 'pandorapool'
        driver.pool_usage = lambda: usage
        return driver

    def test_above_the_floor_is_open(self):
        answer = self.driver({'ok': True, 'free_gib': 9.0, 'total_bytes': 18 << 30,
                              'used_bytes': 9 << 30, 'used_fraction': 0.5,
                              'pool': 'pandorapool'}).capacity(floor_gib=4)
        self.assertTrue(answer['ok'])
        self.assertTrue(answer['measured'])

    def test_below_the_floor_is_closed_with_the_arithmetic(self):
        answer = self.driver({'ok': True, 'free_gib': 1.5, 'total_bytes': 18 << 30,
                              'used_bytes': 17 << 30, 'used_fraction': 0.94,
                              'pool': 'pandorapool'}).capacity(floor_gib=4)
        self.assertFalse(answer['ok'])
        self.assertIn('1.50 GiB free', answer['reason'])
        self.assertIn('4 GiB floor', answer['reason'])

    def test_an_unreadable_pool_does_not_stop_the_worker(self):
        # Refusing every run because the measurement failed is a worse failure
        # than admitting one run too many.
        answer = self.driver({'ok': False, 'error': 'btrfs usage unreadable',
                              'pool': 'pandorapool'}).capacity(floor_gib=4)
        self.assertTrue(answer['ok'])
        self.assertFalse(answer['measured'])


class PoolParsing(unittest.TestCase):
    SAMPLE = '''Overall:
    Device size:\t\t       19327352832
    Device allocated:\t\t       11836325888
    Used:\t\t\t        9126264832
    Free (estimated):\t\t        9777659904\t(min: 6032146432)
'''

    def test_usage_reads_btrfs_rather_than_df(self):
        from pandora.executor import incus
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return 0, self.SAMPLE, ''

        original = incus.run
        incus.run = fake_run
        try:
            driver = incus.IncusDriver.__new__(incus.IncusDriver)
            driver.pool = 'pandorapool'
            usage = driver.pool_usage()
        finally:
            incus.run = original
        self.assertEqual(usage['total_bytes'], 19327352832)
        self.assertEqual(usage['used_bytes'], 9126264832)
        self.assertEqual(usage['free_bytes'], 9777659904)
        self.assertAlmostEqual(usage['free_gib'], 9.11, places=1)
        self.assertIn('btrfs', calls[0])


class Naming(unittest.TestCase):
    def test_a_tagged_image_loses_only_its_tag(self):
        from pandora.executor.incus import untagged
        self.assertEqual(untagged('postgres:16'), 'postgres')
        self.assertEqual(untagged('ghcr.io/neondatabase/wsproxy:latest'),
                         'ghcr.io/neondatabase/wsproxy')
        self.assertEqual(untagged('localhost:5000/thing'), 'localhost:5000/thing')


if __name__ == '__main__':
    unittest.main()
