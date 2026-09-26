"""The worker half, with no worker: manifests, drift, pins, GC policy, parsing."""
import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pandora.errors import ConfigError
from pandora.executor.interface import Receipt, Toolchain
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
            versions.normalize({'nonsense': {}})

    def test_unknown_worker_key_is_refused(self):
        with self.assertRaises(ConfigError):
            versions.normalize({'worker': {'pooll': 'x'}})

    def test_device_must_be_a_dev_path(self):
        with self.assertRaises(ConfigError):
            versions.normalize({'worker': {'device': 'sdb'}})

    def test_digest_ignores_nothing_declared_and_changes_with_a_pin(self):
        one = versions.normalize({})
        two = versions.normalize({'packages': {'incus': '6.0.5-8'}})
        self.assertNotEqual(versions.digest(one), versions.digest(two))
        self.assertEqual(versions.digest(one), versions.digest(versions.normalize({})))

    def test_render_round_trips(self):
        manifest = versions.normalize({'packages': {'incus': '6.0.5-8'},
                                       'worker': {'loop_size_gib': 24}})
        import tomllib
        again = versions.normalize(tomllib.loads(versions.render(manifest)))
        self.assertEqual(versions.digest(manifest), versions.digest(again))

    def test_an_engine_floor_must_be_an_integer(self):
        self.assertIsNone(versions.normalize({})['worker'].get('min_engine_version'))
        self.assertEqual(versions.normalize({'worker': {'min_engine_version': 5}})
                         ['worker']['min_engine_version'], 5)
        for bad in ('5', True):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                versions.normalize({'worker': {'min_engine_version': bad}})

    def test_users_round_trip_and_digest(self):
        key = 'ssh-ed25519 AAAAC3NzaC sterling@laptop'
        manifest = versions.normalize({'users': [{'name': 'sterling', 'key': key}]})
        self.assertEqual(manifest['users'],
                         [{'name': 'sterling', 'key': key, 'role': 'user'}])
        import tomllib
        again = versions.normalize(tomllib.loads(versions.render(manifest)))
        self.assertEqual(versions.digest(manifest), versions.digest(again))
        # A declared user is part of the declaration: the digest covers it.
        bare = versions.normalize({})
        self.assertNotEqual(versions.digest(manifest), versions.digest(bare))

    def test_bad_users_are_refused(self):
        key = 'ssh-ed25519 AAAAC3NzaC sterling@laptop'
        for item in ({'name': 'has space', 'key': key},
                     {'name': 'sterling', 'key': 'not a key'},
                     {'name': 'sterling', 'key': key, 'role': 'boss'},
                     {'name': 'sterling', 'key': key, 'comment': 'x'},
                     {'key': key},
                     'sterling'):
            with self.subTest(item=item), self.assertRaises(ConfigError):
                versions.normalize({'users': [item]})
        with self.assertRaises(ConfigError):        # names must be unique
            versions.normalize({'users': [{'name': 's', 'key': key},
                                          {'name': 's', 'key': key}]})


class AuthorizedKeys(unittest.TestCase):
    """`provision.authorized_lines`: the managed block a manifest becomes."""

    def test_a_user_key_is_pinned_to_the_gateway(self):
        manifest = versions.normalize({'users': [
            {'name': 'sterling', 'key': 'ssh-ed25519 AAAAC3NzaC sterling@laptop'}]})
        lines = provision.authorized_lines(manifest, root='/home/ubuntu/pandora',
                                           engine_root='/home/ubuntu/pandora-engine')
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('restrict,command="'))
        self.assertIn('/home/ubuntu/pandora/bin/gateway --name sterling', lines[0])
        self.assertIn('--engine-root /home/ubuntu/pandora-engine', lines[0])
        self.assertTrue(lines[0].endswith('# pandora:sterling'))

    def test_an_admin_key_is_a_plain_line(self):
        manifest = versions.normalize({'users': [
            {'name': 'pat', 'role': 'admin', 'key': 'ssh-ed25519 AAAA pat@work'}]})
        lines = provision.authorized_lines(manifest, root='/w', engine_root='/e')
        self.assertEqual(lines, ['ssh-ed25519 AAAA pat@work # pandora:pat'])

    def test_the_preamble_carries_the_gateway_the_users_and_the_floor(self):
        import base64
        import re
        from pandora.engine import service
        manifest = versions.normalize({})
        text = provision.preamble(manifest, root='/w', engine_root='/e',
                                  pool_file='/w/pool.img')
        # Only single-line assignments match; MANIFEST's value is multi-line TOML.
        values = dict(re.findall(r'(?m)^(\w+)=(\'[^\']*\'|\S+)$', text))
        unquote = lambda v: v[1:-1] if v.startswith("'") else v   # noqa: E731
        self.assertEqual(unquote(values['MIN_ENGINE']), str(service.ENGINE_VERSION))
        decoded = base64.b64decode(unquote(values['GATEWAY_B64'])).decode()
        self.assertIn('SSH_ORIGINAL_COMMAND', decoded)
        self.assertEqual(base64.b64decode(unquote(values['USERS_B64'])), b'')


class Drift(unittest.TestCase):
    def setUp(self):
        self.manifest = versions.normalize({'packages': {'incus': '6.0.5-8', 'git': '*'}})

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
        manifest = versions.normalize({'packages': {'incus': '6.0.5-8'}})
        text = provision.preamble(manifest, root='/w', engine_root='/e', pool_file='/p')
        self.assertIn('incus=6.0.5-8', text)


class FakeDriver:
    """Enough of IncusDriver for the sweeps, and nothing that touches a host."""

    def __init__(self, instances, project='pandora', pool='pandorapool'):
        self.project, self.pool = project, pool
        self._instances = list(instances)
        self.destroyed = []

    listing_fails = False

    def instances(self, *, check=False):
        if self.listing_fails:
            if check:
                raise RuntimeError('incus list exited 1: connection refused')
            return []
        return [dict(item) for item in self._instances]

    def qgroups(self):
        return {'containers/%s_%s' % (self.project, item['name']): (4 << 30, 1 << 20)
                for item in self._instances}

    def incus(self, *args, **kwargs):
        return 0, '', ''

    def pool_usage(self):
        return {'ok': True, 'pool': self.pool, 'free_gib': 9.0, 'total_bytes': 18 << 30,
                'used_bytes': 9 << 30, 'used_fraction': 0.5}

    def destroy(self, instance):
        self.destroyed.append(instance.name)
        self._instances = [item for item in self._instances if item['name'] != instance.name]
        return Receipt(run_id=instance.run_id, instance=instance.name, seconds=0.5,
                       instance_gone=True, volume_gone=True, veth_gone=True, cgroup_gone=True)

    def settle_qgroups(self):
        return False


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

    @classmethod
    def rebuilt(cls, family, version, **extra):
        """One toolchain family, rebuilt: same source_id, a different install."""
        return cls.spec(family, install_command='pnpm i # v%d' % version, **extra)

    @staticmethod
    def name_of(spec):
        from pandora.engine.runner import toolchain_of
        return 'golden-' + toolchain_of(spec).fingerprint()

    @staticmethod
    def enrolled(*pairs):
        """Family keys for `sweep`'s `enrolled`: (repo, source_id)."""
        return {(repo, 'source:' + source_id) for repo, source_id in pairs}

    def test_the_index_names_the_repo_and_the_last_use(self):
        names = self.goldens_for('a', 'b')
        self.attempt('r1', 'acme', 'finished', self.spec('a'), 100.0)
        self.attempt('r2', 'acme', 'finished', self.spec('b'), 200.0)
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names])
        from pandora.engine.runner import Paths
        rows = goldens.index(Paths(self.root), driver)
        self.assertEqual([row['name'] for row in rows], [names[1], names[0]])
        self.assertEqual(rows[0]['repo'], 'acme')
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
        self.attempt('live', 'acme', 'running', self.spec('a'), time.time())
        driver = FakeDriver([{'name': 'run-live', 'state': 'RUNNING', 'created': ''},
                             {'name': 'run-leaked', 'state': 'RUNNING', 'created': ''}])
        receipt = gc.sweep(self.root, driver, keep=2)
        self.assertEqual(driver.destroyed, ['run-leaked'])
        self.assertTrue(receipt['ok'])
        self.assertEqual([item['name'] for item in receipt['removed']], ['run-leaked'])

    def test_gc_keeps_the_newest_goldens_per_family(self):
        specs = [self.rebuilt('a', version) for version in (1, 2, 3)]
        names = [self.name_of(spec) for spec in specs]
        for index, (spec, at) in enumerate(zip(specs, (100.0, 200.0, 300.0))):
            self.attempt('r%d' % index, 'acme', 'finished', spec, at)
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names])
        receipt = gc.sweep(self.root, driver, keep=2,
                           enrolled=self.enrolled(('acme', 'a')))
        self.assertEqual(driver.destroyed, [names[0]], 'the least recently used goes')
        self.assertEqual({item['name'] for item in receipt['kept']}, {names[1], names[2]})

    def test_keep_one_keeps_one_golden_per_toolchain_not_per_repo(self):
        """Issue #81: two toolchains in one repository are two families."""
        journeys = [self.rebuilt('acme-journeys', version) for version in (1, 2)]
        surfaces = [self.rebuilt('acme-surfaces', version) for version in (1, 2)]
        # The surfaces family is used rarely: both of its goldens are older
        # than either journeys golden, which is what ranked it out on 2026-09-23.
        for index, (spec, at) in enumerate(((surfaces[0], 10.0), (surfaces[1], 20.0),
                                            (journeys[0], 300.0), (journeys[1], 400.0))):
            self.attempt('r%d' % index, 'acme', 'finished', spec, at)
        names = [self.name_of(spec) for spec in journeys + surfaces]
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names])
        receipt = gc.sweep(self.root, driver, keep=1,
                           enrolled=self.enrolled(('acme', 'acme-journeys'),
                                                  ('acme', 'acme-surfaces')))
        self.assertEqual(sorted(driver.destroyed), sorted([names[0], names[2]]))
        kept = {item['name']: item for item in receipt['kept']}
        self.assertEqual(set(kept), {names[1], names[3]})
        self.assertIn('acme acme-surfaces', kept[names[3]]['why'])

    def test_a_toolchain_without_a_source_id_is_its_own_family(self):
        specs = [self.rebuilt('', version) for version in (1, 2)]
        for index, spec in enumerate(specs):
            self.attempt('r%d' % index, 'acme', 'finished', spec, 100.0 * (index + 1))
        driver = FakeDriver([{'name': self.name_of(spec), 'state': 'STOPPED', 'created': ''}
                             for spec in specs])
        # A fingerprint-named family cannot arrive through `--family`, so the
        # enrollment here is written in the family key's own shape.
        enrolled = {('acme', 'fingerprint:' + self.name_of(spec)[len('golden-'):])
                    for spec in specs}
        gc.sweep(self.root, driver, keep=1, enrolled=enrolled)
        self.assertEqual(driver.destroyed, [])

    def test_a_golden_an_enrolled_config_names_is_never_removed(self):
        specs = [self.rebuilt('a', version) for version in (1, 2, 3)]
        names = [self.name_of(spec) for spec in specs]
        for index, (spec, at) in enumerate(zip(specs, (100.0, 200.0, 300.0))):
            self.attempt('r%d' % index, 'acme', 'finished', spec, at)
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names])
        oldest = names[0][len('golden-'):]
        receipt = gc.sweep(self.root, driver, keep=1,
                           protect=gc.parse_protect([oldest + '=acme']))
        self.assertEqual(driver.destroyed, [names[1]])
        whys = {item['name']: item['why'] for item in receipt['kept']}
        self.assertEqual(whys[names[0]], 'named by acme pandora.toml')
        self.assertEqual(receipt['protect'], {oldest: 'acme'})

    def test_protect_guards_a_golden_no_attempt_explains(self):
        driver = FakeDriver([{'name': 'golden-%016x' % n, 'state': 'STOPPED', 'created': ''}
                             for n in (1, 2)])
        gc.sweep(self.root, driver, keep=0, protect=gc.parse_protect(['golden-%016x' % 1]))
        self.assertEqual(driver.destroyed, ['golden-%016x' % 2])

    def test_a_pinned_golden_is_never_removed(self):
        specs = [self.rebuilt('a', 1, pins={'base_image': 'abc'}), self.rebuilt('a', 2)]
        for index, spec in enumerate(specs):
            self.attempt('r%d' % index, 'acme', 'finished', spec, 100.0 * (index + 1))
        driver = FakeDriver([{'name': self.name_of(spec), 'state': 'STOPPED', 'created': ''}
                             for spec in specs])
        receipt = gc.sweep(self.root, driver, keep=1,
                           enrolled=self.enrolled(('acme', 'a')))
        self.assertEqual(driver.destroyed, [])
        self.assertIn('pinned', {item['name']: item['why']
                                 for item in receipt['kept']}[self.name_of(specs[0])])

    def test_parse_protect_takes_a_name_or_a_fingerprint(self):
        self.assertEqual(gc.parse_protect(['golden-abc=acme', 'def', '']),
                         {'abc': 'acme', 'def': None})

    def test_gc_never_removes_a_golden_a_live_attempt_needs(self):
        specs = [self.rebuilt('a', version) for version in (1, 2, 3)]
        names = [self.name_of(spec) for spec in specs]
        self.attempt('r0', 'acme', 'running', specs[0], 100.0)
        self.attempt('r1', 'acme', 'finished', specs[1], 200.0)
        self.attempt('r2', 'acme', 'finished', specs[2], 300.0)
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names])
        receipt = gc.sweep(self.root, driver, keep=1,
                           enrolled=self.enrolled(('acme', 'a')))
        self.assertEqual(driver.destroyed, [names[1]])
        whys = {item['name']: item['why'] for item in receipt['kept']}
        self.assertIn('live attempt', whys[names[0]])

    def test_a_queued_attempt_keeps_its_golden_too(self):
        # Queued is live: the attempt is admitted-to-be and will clone this
        # golden when a lane frees. Removing it then fails the run as
        # prepare-failed, or rebuilds a golden for minutes.
        specs = [self.rebuilt('a', version) for version in (1, 2)]
        names = [self.name_of(spec) for spec in specs]
        self.attempt('r0', 'acme', 'queued', specs[0], 100.0)
        self.attempt('r1', 'acme', 'finished', specs[1], 200.0)
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names])
        receipt = gc.sweep(self.root, driver, keep=1,
                           enrolled=self.enrolled(('acme', 'a')))
        self.assertEqual(driver.destroyed, [])
        whys = {item['name']: item['why'] for item in receipt['kept']}
        self.assertIn('live attempt', whys[names[0]])

    def test_dry_run_removes_nothing_and_still_says_what_it_would(self):
        specs = [self.rebuilt('a', version) for version in (1, 2, 3)]
        names = [self.name_of(spec) for spec in specs]
        for index, (spec, at) in enumerate(zip(specs, (100.0, 200.0, 300.0))):
            self.attempt('r%d' % index, 'acme', 'finished', spec, at)
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names] +
                            [{'name': 'run-leaked', 'state': 'RUNNING', 'created': ''}])
        receipt = gc.sweep(self.root, driver, keep=2, dry_run=True,
                           enrolled=self.enrolled(('acme', 'a')))
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

    def test_a_failed_instance_listing_aborts_the_sweep(self):
        """#88: `incus list` failing read as an empty project, so every volume
        looked leaked and gc deleted them all."""
        deletes = []

        class Listless(FakeDriver):
            listing_fails = True

            def incus(self, *args, **kwargs):
                if args[:3] == ('storage', 'volume', 'list'):
                    return 0, 'container,run-live,\ncontainer,golden-x,\n', ''
                deletes.append(args)
                return 0, '', ''
        driver = Listless([{'name': 'run-leaked', 'state': 'RUNNING', 'created': ''}])
        receipt = gc.sweep(self.root, driver, keep=0)
        self.assertFalse(receipt['ok'])
        self.assertIn('incus list failed', receipt['reason'])
        self.assertEqual((driver.destroyed, deletes), ([], []))
        self.assertEqual(receipt['removed'], [])

    def test_a_golden_whose_destroy_left_objects_is_not_removed(self):
        from pandora.executor.interface import DestroyIncomplete
        specs = [self.rebuilt('a', version) for version in (1, 2)]
        names = [self.name_of(spec) for spec in specs]
        for index, (spec, at) in enumerate(zip(specs, (100.0, 200.0))):
            self.attempt('r%d' % index, 'acme', 'finished', spec, at)

        class Leaky(FakeDriver):
            def destroy(self, instance):
                raise DestroyIncomplete('destroy of %s left objects' % instance.name,
                                        {'volume_gone': False})
        receipt = gc.sweep(self.root, Leaky([{'name': name, 'state': 'STOPPED',
                                              'created': ''} for name in names]),
                           keep=1, enrolled=self.enrolled(('acme', 'a')))
        self.assertFalse(receipt['ok'])
        self.assertEqual(receipt['removed'], [])
        self.assertEqual([(item['name'], item['removed']) for item in receipt['failed']],
                         [(names[0], False)])
        self.assertEqual(receipt['freed_bytes'], 0)

    def test_an_orphaned_family_is_collected_whole_once_past_its_grace(self):
        """#116: a family no enrolled config names does not keep its slots."""
        specs = [self.rebuilt('a', version) for version in (1, 2)]
        names = [self.name_of(spec) for spec in specs]
        for index, spec in enumerate(specs):
            self.attempt('r%d' % index, 'acme', 'finished', spec, 100.0 * (index + 1))
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names])
        receipt = gc.sweep(self.root, driver, keep=2, enrolled=set())
        self.assertEqual(sorted(driver.destroyed), sorted(names),
                         'an orphaned family past its grace loses every member')
        whys = {item['name']: item['why'] for item in receipt['removed']}
        self.assertIn('no enrolled config', whys[names[0]])

    def test_an_orphaned_family_inside_its_grace_is_kept(self):
        now = time.time()
        specs = [self.rebuilt('a', version) for version in (1, 2)]
        names = [self.name_of(spec) for spec in specs]
        for index, (spec, at) in enumerate(zip(specs, (now - 7200, now - 60))):
            self.attempt('r%d' % index, 'acme', 'finished', spec, at)
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names])
        receipt = gc.sweep(self.root, driver, keep=2, enrolled=set())
        self.assertEqual(driver.destroyed, [])
        whys = {item['name']: item['why'] for item in receipt['kept']}
        self.assertIn('no enrolled config', whys[names[0]])
        self.assertIn('collectable in', whys[names[0]])

    def test_an_enrolled_family_keeps_its_slots_but_orphans_do_not(self):
        """The same family is ranked when enrolled and collected when not."""
        specs = [self.rebuilt('a', version) for version in (1, 2, 3)]
        names = [self.name_of(spec) for spec in specs]
        for index, (spec, at) in enumerate(zip(specs, (100.0, 200.0, 300.0))):
            self.attempt('r%d' % index, 'acme', 'finished', spec, at)
        enrolled_driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                                      for name in names])
        gc.sweep(self.root, enrolled_driver, keep=2,
                 enrolled=self.enrolled(('acme', 'a')))
        self.assertEqual(enrolled_driver.destroyed, [names[0]])
        orphan_driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                                    for name in names])
        gc.sweep(self.root, orphan_driver, keep=2, enrolled=set())
        self.assertEqual(sorted(orphan_driver.destroyed), sorted(names))

    def test_a_sweep_with_no_enrollment_data_collects_no_orphans(self):
        """`enrolled=None` is "nobody could say", not "nothing is enrolled":
        a bare `gc` on the worker gets the keep ranking for every family,
        however ancient."""
        specs = [self.rebuilt('a', version) for version in (1, 2, 3)]
        names = [self.name_of(spec) for spec in specs]
        for index, (spec, at) in enumerate(zip(specs, (100.0, 200.0, 300.0))):
            self.attempt('r%d' % index, 'acme', 'finished', spec, at)
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names])
        receipt = gc.sweep(self.root, driver, keep=2)
        self.assertEqual(driver.destroyed, [names[0]],
                         'only the rank past keep goes, even past the grace')
        whys = {item['name']: item['why'] for item in receipt['kept']}
        self.assertIn('most recently used', whys[names[2]])
        self.assertIsNone(receipt['enrolled'],
                          'null, not [], records that no data arrived')

    def test_an_empty_enrollment_is_an_answer_not_a_lack_of_one(self):
        """`enrolled=set()` is the client saying its configs name nothing."""
        specs = [self.rebuilt('a', version) for version in (1, 2)]
        names = [self.name_of(spec) for spec in specs]
        for index, spec in enumerate(specs):
            self.attempt('r%d' % index, 'acme', 'finished', spec,
                         100.0 * (index + 1))
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names])
        receipt = gc.sweep(self.root, driver, keep=2, enrolled=set())
        self.assertEqual(sorted(driver.destroyed), sorted(names))
        self.assertEqual(receipt['enrolled'], [])

    def test_an_unknown_golden_ages_out_by_when_incus_says_it_was_built(self):
        """A golden no attempt explains has no last use; its clock is `created`."""
        old = FakeDriver([{'name': 'golden-deadbeefdeadbeef', 'state': 'STOPPED',
                           'created': '2020/01/01 00:00 UTC'}])
        gc.sweep(self.root, old, keep=2, enrolled=set())
        self.assertEqual(old.destroyed, ['golden-deadbeefdeadbeef'])
        fresh = FakeDriver([{'name': 'golden-deadbeefdeadbeef', 'state': 'STOPPED',
                             'created': time.strftime('%Y/%m/%d %H:%M UTC',
                                                      time.gmtime())}])
        receipt = gc.sweep(self.root, fresh, keep=2, enrolled=set())
        self.assertEqual(fresh.destroyed, [])
        self.assertIn('no enrolled config', receipt['kept'][0]['why'])

    def test_an_orphaned_golden_with_no_clock_at_all_is_kept(self):
        """The don't-guess rule: an age that cannot be read is not an old age."""
        driver = FakeDriver([{'name': 'golden-deadbeefdeadbeef', 'state': 'STOPPED',
                              'created': ''}])
        receipt = gc.sweep(self.root, driver, keep=0, enrolled=set())
        self.assertEqual(driver.destroyed, [])
        self.assertIn('age is unknown', receipt['kept'][0]['why'])

    def test_an_unknown_age_member_does_not_drag_its_family_in(self):
        """Staleness is per member: a sibling's old clock collects only itself."""
        driver = FakeDriver([{'name': 'golden-aaaaaaaaaaaaaaaa', 'state': 'STOPPED',
                              'created': '2020/01/01 00:00 UTC'},
                             {'name': 'golden-bbbbbbbbbbbbbbbb', 'state': 'STOPPED',
                              'created': ''}])
        receipt = gc.sweep(self.root, driver, keep=0, enrolled=set())
        self.assertEqual(driver.destroyed, ['golden-aaaaaaaaaaaaaaaa'])
        whys = {item['name']: item['why'] for item in receipt['kept']}
        self.assertIn('age is unknown', whys['golden-bbbbbbbbbbbbbbbb'])

    def test_the_orphan_rule_never_reaches_another_callers_repo(self):
        """A shared worker: one client's enrollment cannot orphan a family in
        a repository it never enrolled -- that family gets the keep ranking,
        exactly as if no enrollment data had arrived for it."""
        specs = [self.rebuilt('a', 1), self.rebuilt('b', 1), self.rebuilt('c', 1)]
        names = [self.name_of(spec) for spec in specs]
        self.attempt('r0', 'acme', 'finished', specs[0], 100.0)
        self.attempt('r1', 'acme', 'finished', specs[1], 100.0)
        self.attempt('r2', 'other-repo', 'finished', specs[2], 100.0)
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names])
        receipt = gc.sweep(self.root, driver, keep=2,
                           enrolled=self.enrolled(('acme', 'a')),
                           repos={'acme'})
        # Only acme's unnamed family is orphaned; other-repo's is wanted.
        self.assertEqual(driver.destroyed, [names[1]])
        whys = {item['name']: item['why'] for item in receipt['kept']}
        self.assertIn('most recently used', whys[names[2]])
        self.assertEqual(receipt['repos'], ['acme'])

    def test_an_orphan_rule_without_a_repos_claim_covers_everything(self):
        """repos=None is a caller that did not say: every family is covered."""
        spec = self.rebuilt('a', 1)
        self.attempt('r0', 'other-repo', 'finished', spec, 100.0)
        driver = FakeDriver([{'name': self.name_of(spec), 'state': 'STOPPED',
                              'created': ''}])
        gc.sweep(self.root, driver, keep=2, enrolled=set())
        self.assertEqual(driver.destroyed, [self.name_of(spec)])

    def test_a_golden_claimed_between_the_snapshot_and_the_delete_is_kept(self):
        """The live-golden set is a snapshot; re-ask before the destroy."""
        spec = self.rebuilt('a', 1)
        name = self.name_of(spec)
        self.attempt('r0', 'acme', 'finished', spec, 100.0)
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}])
        calls = []

        def live_later(paths):
            calls.append(1)
            # The second read -- the re-check -- sees the attempt that
            # submitted while the sweep ran.
            return set() if len(calls) == 1 else {name}

        with mock.patch.object(gc.golden_index, 'live_goldens', live_later):
            receipt = gc.sweep(self.root, driver, keep=0, enrolled=set())
        self.assertEqual(driver.destroyed, [])
        self.assertIn('mid-sweep', receipt['kept'][0]['why'])

    def test_a_live_recheck_that_fails_keeps_the_golden(self):
        """A re-check that cannot answer is a keep, never a delete."""
        spec = self.rebuilt('a', 1)
        name = self.name_of(spec)
        self.attempt('r0', 'acme', 'finished', spec, 100.0)
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}])
        calls = []

        def live_broken(paths):
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError('ledger went away')
            return set()

        with mock.patch.object(gc.golden_index, 'live_goldens', live_broken):
            receipt = gc.sweep(self.root, driver, keep=0, enrolled=set())
        self.assertEqual(driver.destroyed, [])
        self.assertIn('re-check failed', receipt['kept'][0]['why'])

    def test_drop_family_removes_an_enrolled_family_on_sight(self):
        specs = [self.rebuilt('a', version) for version in (1, 2)]
        names = [self.name_of(spec) for spec in specs]
        for index, spec in enumerate(specs):
            self.attempt('r%d' % index, 'acme', 'finished', spec,
                         time.time() - 60 * (index + 1))
        driver = FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                             for name in names])
        receipt = gc.sweep(self.root, driver, keep=2,
                           enrolled=self.enrolled(('acme', 'a')),
                           drop={'acme a'})
        self.assertEqual(sorted(driver.destroyed), sorted(names))
        self.assertIn('--drop-family', receipt['removed'][0]['why'])

    def test_drop_family_still_keeps_a_pinned_golden(self):
        spec = self.rebuilt('a', 1, pins={'base_image': 'abc'})
        self.attempt('r0', 'acme', 'finished', spec, time.time() - 60)
        driver = FakeDriver([{'name': self.name_of(spec), 'state': 'STOPPED',
                              'created': ''}])
        gc.sweep(self.root, driver, keep=0, drop={'acme a'})
        self.assertEqual(driver.destroyed, [])

    def test_a_drop_family_that_names_nothing_is_reported(self):
        driver = FakeDriver([])
        receipt = gc.sweep(self.root, driver, keep=2, drop={'nope x'})
        self.assertFalse(receipt['ok'])
        self.assertEqual([(item['kind'], item['name']) for item in receipt['failed']],
                         [('family', 'nope x')])

    def test_parse_families_takes_repo_equals_source_id(self):
        self.assertEqual(gc.parse_families(['acme=a', ' other = x ']),
                         {('acme', 'source:a'), ('other', 'source:x')})

    def test_a_malformed_family_value_is_an_error_not_an_empty_answer(self):
        """A dropped --family reads as "names nothing": refuse it instead."""
        for bad in ('nope', '', '=x', 'repo=', ' = '):
            with self.assertRaises(ValueError):
                gc.parse_families([bad])

    def test_a_malformed_family_flag_fails_argparse(self):
        from pandora.worker import service
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                service.main(['--root', str(self.root), 'gc', '--family', 'oops'])

    def test_a_volume_whose_instance_appears_before_the_delete_is_kept(self):
        """#88: the instance re-check sits between the listings and the delete."""
        deleted = []

        class Racing(FakeDriver):
            listings = 0

            def instances(self, *, check=False):
                self.listings += 1
                rows = super().instances(check=check)
                # The clone lands after the sweep's own listings and before
                # the per-volume re-check.
                if self.listings < 3:
                    return [row for row in rows if row['name'] != 'run-new']
                return rows

            def incus(self, *args, **kwargs):
                if args[:3] == ('storage', 'volume', 'list'):
                    return 0, 'container,run-new,\ncontainer,run-gone,\n', ''
                if args[:3] == ('storage', 'volume', 'delete'):
                    deleted.append(args[4])
                    return 0, '', ''
                return 1, '', ''

        driver = Racing([{'name': 'run-new', 'state': 'RUNNING', 'created': ''}])
        receipt = gc.sweep(self.root, driver, keep=2)
        self.assertEqual(deleted, ['container/run-gone'])
        self.assertEqual(driver.destroyed, [])
        whys = {item['name']: item['why'] for item in receipt['kept']}
        self.assertIn('appeared', whys['run-new'])

    def test_a_failed_volume_listing_is_a_failure_not_a_clean_sweep(self):
        """A nonzero `incus storage volume list` must mark the receipt failed."""
        deleted = []

        class Mute(FakeDriver):
            def incus(self, *args, **kwargs):
                if args[:3] == ('storage', 'volume', 'list'):
                    return 3, '', 'database is locked'
                deleted.append(args)
                return 0, '', ''

        receipt = gc.sweep(self.root, Mute([]), keep=2)
        self.assertFalse(receipt['ok'])
        self.assertEqual([(item['kind'], item['name']) for item in receipt['failed']],
                         [('listing', 'incus list')])
        self.assertIn('database is locked', receipt['failed'][0]['why'])
        self.assertEqual(deleted, [])

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


class QgroupSettling(unittest.TestCase):
    """A quota is only as good as the accounting under it."""
    CLEAN = '0/259  4263026688  37650432  none  none  containers/pandora_golden-a\n'
    DIRTY = 'WARNING: qgroup data inconsistent, rescan recommended\n'

    def settle(self, answers):
        from pandora.executor import incus
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if 'rescan' in argv:
                return 0, 'quota rescan started', ''
            return 0, self.CLEAN, answers.pop(0) if answers else ''

        original = incus.run
        incus.run = fake_run
        try:
            driver = incus.IncusDriver.__new__(incus.IncusDriver)
            driver.pool = 'pandorapool'
            return driver.settle_qgroups(), calls
        finally:
            incus.run = original

    def test_consistent_accounting_costs_one_read_and_no_rescan(self):
        rescanned, calls = self.settle([''])
        self.assertFalse(rescanned)
        self.assertEqual(len(calls), 1)

    def test_inconsistent_accounting_is_rescanned_before_a_limit_is_set(self):
        rescanned, calls = self.settle([self.DIRTY, ''])
        self.assertTrue(rescanned)
        self.assertTrue(any('rescan' in argv and '-w' in argv for argv in calls))

    def test_accounting_that_will_not_settle_refuses_the_quota(self):
        from pandora.executor.interface import CloneFailed
        with self.assertRaises(CloneFailed):
            self.settle([self.DIRTY, self.DIRTY])

    def test_a_rescan_timeout_is_a_clone_failure_not_an_engine_error(self):
        """#88: TimeoutExpired must not reach the run path's generic handler."""
        import subprocess
        from pandora.executor import incus
        from pandora.executor.interface import CloneFailed

        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get('timeout'))

        original = incus.run
        incus.run = fake_run
        try:
            driver = incus.IncusDriver.__new__(incus.IncusDriver)
            driver.pool = 'pandorapool'
            with self.assertRaises(CloneFailed) as caught:
                driver.settle_qgroups()
        finally:
            incus.run = original
        self.assertIn('timed out', str(caught.exception))


class Naming(unittest.TestCase):
    def test_a_tagged_image_loses_only_its_tag(self):
        from pandora.executor.incus import untagged
        self.assertEqual(untagged('postgres:16'), 'postgres')
        self.assertEqual(untagged('ghcr.io/neondatabase/wsproxy:latest'),
                         'ghcr.io/neondatabase/wsproxy')
        self.assertEqual(untagged('localhost:5000/thing'), 'localhost:5000/thing')


if __name__ == '__main__':
    unittest.main()
