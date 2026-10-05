"""Golden pinning: the worker names a golden by its base image and lockfiles.

No Incus anywhere: the image lookup is a fake on the driver, and the cache
lives in a scratch engine root.
"""
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora.engine import pinning, runner
from pandora.engine.ledger import Ledger
from pandora.executor.incus import IncusDriver
from pandora.tests.test_engine import PLAN
from pandora.worker import canary, gc, goldens, pins

RECIPE = dict(PLAN['worker'])


class Images:
    """The two driver methods pinning reads, faked."""

    def __init__(self, image='a' * 64, built=()):
        self.image, self.built, self.lookups = image, set(built), []

    def image_fingerprint(self, alias):
        self.lookups.append(alias)
        if isinstance(self.image, Exception):
            raise self.image
        return self.image

    def exists(self, name):
        return name in self.built

    def golden_name(self, toolchain):
        return 'golden-' + toolchain.fingerprint()


def name_of(spec):
    return 'golden-' + runner.toolchain_of(spec).fingerprint()


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'engine'
        self.paths = runner.Paths(self.root).ensure()
        self.source = self.root / 'src' / 'demo' / 'latest'
        self.source.mkdir(parents=True)

    def settle(self, driver, source=None, **kw):
        return pinning.settle(RECIPE, self.root, driver, name_of,
                              source=str(source or self.source), **kw)

    def fingerprint(self, driver, source=None):
        return runner.toolchain_of(self.settle(driver, source)).fingerprint()


class Fingerprint(Scratch):
    def test_it_changes_with_the_image_and_with_nothing_else_unrelated(self):
        (self.source / 'pnpm-lock.yaml').write_text('lock v1\n')
        first = self.fingerprint(Images('a' * 64))
        self.assertEqual(len(first), 16)
        (self.source / 'README.md').write_text('not a lockfile\n')
        (self.source / 'pkg').mkdir()
        (self.source / 'pkg' / 'pnpm-lock.yaml').write_text('a fixture\n')
        self.assertEqual(self.fingerprint(Images('a' * 64)), first)
        self.assertNotEqual(self.fingerprint(Images('b' * 64)), first)

    def test_it_changes_with_every_root_lockfile(self):
        names = ('pnpm-lock.yaml', 'package-lock.json', 'yarn.lock', 'uv.lock',
                 'poetry.lock', 'Cargo.lock', 'go.sum', 'Gemfile.lock',
                 'requirements.txt', 'requirements-dev.txt')
        for name in names:
            (self.source / name).write_text('v1 ' + name)
        seen = {self.fingerprint(Images())}
        for name in names:
            (self.source / name).write_text('v2 ' + name)
            seen.add(self.fingerprint(Images()))
        self.assertEqual(len(seen), len(names) + 1)

    def test_a_new_lockfile_changes_it(self):
        before = self.fingerprint(Images())
        (self.source / 'uv.lock').write_text('x')
        self.assertNotEqual(self.fingerprint(Images()), before)

    def test_nothing_resolved_names_the_recipe_golden(self):
        """No lookup and no lockfile: the name an unpinned attempt always had."""
        spec = self.settle(Images(RuntimeError('no route')))
        self.assertEqual(spec['pins'], {})
        self.assertEqual(name_of(spec), name_of(RECIPE))
        self.assertIn('unresolved', spec['pin_notes'][0])

    def test_golden_pins_is_the_payload_shape(self):
        (self.source / 'go.sum').write_text('sum\n')
        spec = self.settle(Images('c' * 64))
        self.assertEqual(pinning.golden_pins(spec), {
            'image': 'c' * 64, 'lockfiles': {'go.sum': hashlib.sha256(b'sum\n').hexdigest()}})
        self.assertIsNone(pinning.golden_pins(RECIPE))

    def test_settle_is_idempotent(self):
        spec = self.settle(Images('a' * 64))
        again = pinning.settle(spec, self.root, Images('b' * 64), name_of,
                               source=str(self.source))
        self.assertEqual(again['pins'], spec['pins'])


class ImageCache(Scratch):
    def test_a_fresh_answer_is_reused_inside_the_ttl(self):
        driver = Images('a' * 64)
        pinning.image(self.root, 'images:x', driver.image_fingerprint, now=1000.0)
        driver.image = 'b' * 64
        found, how = pinning.image(self.root, 'images:x', driver.image_fingerprint,
                                   now=1000.0 + pinning.IMAGE_TTL - 1)
        self.assertEqual((found, how), ('a' * 64, 'cached'))
        self.assertEqual(len(driver.lookups), 1)

    def test_an_expired_answer_is_looked_up_again(self):
        driver = Images('a' * 64)
        pinning.image(self.root, 'images:x', driver.image_fingerprint, now=1000.0)
        driver.image = 'b' * 64
        found, how = pinning.image(self.root, 'images:x', driver.image_fingerprint,
                                   now=1000.0 + pinning.IMAGE_TTL + 1)
        self.assertEqual((found, how), ('b' * 64, 'resolved'))

    def test_a_failed_lookup_falls_back_to_the_last_answer(self):
        driver = Images('a' * 64)
        pinning.image(self.root, 'images:x', driver.image_fingerprint, now=1000.0)
        driver.image = RuntimeError('image server down')
        found, how = pinning.image(self.root, 'images:x', driver.image_fingerprint,
                                   now=1000.0 + pinning.IMAGE_TTL + 1)
        self.assertEqual(found, 'a' * 64)
        self.assertTrue(how.startswith('stale: image server down'), how)

    def test_a_cached_image_whose_golden_is_unbuilt_is_looked_up_again(self):
        """The build is cold anyway, so it takes the newest image."""
        old = Images('a' * 64)
        first = self.settle(old)
        new = Images('b' * 64, built=())
        second = self.settle(new)
        self.assertEqual(second['pins']['base_image'], 'b' * 64)
        self.assertNotEqual(name_of(first), name_of(second))

    def test_a_cached_image_whose_golden_is_built_is_kept(self):
        first = self.settle(Images('a' * 64))
        warm = Images('b' * 64, built={name_of(first)})
        second = self.settle(warm)
        self.assertEqual(name_of(second), name_of(first))
        self.assertEqual(warm.lookups, [])


class Agreement(Scratch):
    """Every place that names a golden names the same one for the same inputs."""

    def setUp(self):
        super().setUp()
        (self.source / 'pnpm-lock.yaml').write_text('lock\n')

    def routed(self, driver):
        attempt = self.paths.attempt('r1')
        attempt.mkdir(parents=True, exist_ok=True)
        (attempt / 'toolchain.json').write_text(json.dumps(RECIPE))
        return name_of(runner.settle_toolchain(self.paths, 'r1', str(self.source), driver))

    def test_worker_pins_agrees_with_the_routed_name(self):
        driver = Images('d' * 64)
        with mock.patch.object(pins, 'registry_digest', return_value='sha256:' + 'e' * 64):
            answer = pins.resolve(dict(RECIPE, service_images=['postgres:16']), self.root,
                                  driver, source=str(self.source))
        routed = self.routed(driver)
        # The service image is a recipe field, so compare against that recipe.
        self.assertEqual(answer['fingerprint_recipe'],
                         runner.toolchain_of(dict(RECIPE, service_images=['postgres:16']))
                         .fingerprint())
        self.assertEqual(answer['service_digests'], {'postgres:16': 'sha256:' + 'e' * 64})
        self.assertNotIn('service:postgres:16', answer['pins'])
        plain = pins.resolve(dict(RECIPE), self.root, driver, source=str(self.source))
        self.assertEqual(plain['golden'], routed)
        self.assertTrue(plain['ok'], plain)
        self.assertEqual(plain['golden_pins']['image'], 'd' * 64)

    def test_the_worker_service_verb_uses_the_engine_root_cache(self):
        from pandora.worker import service
        toolchain = Path(self.tmp.name) / 'toolchain.json'
        toolchain.write_text(json.dumps(RECIPE))
        driver = Images('d' * 64)
        routed = self.routed(driver)
        driver.image = 'f' * 64              # cached for the TTL: the routed answer holds
        driver.built.add(routed)
        out = io.StringIO()
        with mock.patch.object(service, 'driver_for', return_value=driver), \
                contextlib.redirect_stdout(out):
            service.main(['--root', str(Path(self.tmp.name) / 'worker'),
                          '--engine-root', str(self.root), 'pins',
                          '--toolchain', str(toolchain), '--source', str(self.source)])
        self.assertEqual(json.loads(out.getvalue())['golden'], routed)

    def test_the_engine_golden_verb_agrees_with_the_routed_name(self):
        from pandora.engine import service
        driver = Images('d' * 64)
        routed = self.routed(driver)
        driver.built.add(routed)
        driver.warm = lambda name: name in driver.built
        request = {'worker': RECIPE, 'lockfiles': pinning.lockfiles(self.source)}
        out = io.StringIO()
        with mock.patch('pandora.executor.incus.IncusDriver', return_value=driver), \
                mock.patch('sys.stdin', io.StringIO(json.dumps(request))), \
                contextlib.redirect_stdout(out):
            service.main(['--root', str(self.root), 'golden'])
        answer = json.loads(out.getvalue())
        self.assertEqual(answer['golden'], routed)
        self.assertTrue(answer['warm'])
        self.assertEqual(answer['recipe'], runner.toolchain_of(RECIPE).fingerprint())

    def test_a_fanout_child_inherits_the_parents_name(self):
        driver = Images('d' * 64)
        parent = self.routed(driver)
        child = self.paths.attempt('r2')
        child.mkdir()
        (child / 'toolchain.json').write_text(
            (self.paths.attempt('r1') / 'toolchain.json').read_text())
        driver.image = 'f' * 64
        self.assertEqual(name_of(runner.settle_toolchain(self.paths, 'r2', str(self.source),
                                                         driver)), parent)


class Launch(unittest.TestCase):
    def launched(self, base_image, pin):
        from pandora.executor.interface import Toolchain
        driver = IncusDriver(root=tempfile.gettempdir())
        calls = []

        def incus(*args, **kw):
            calls.append(args)
            raise RuntimeError('stop after launch')
        driver.exists = lambda name: False
        driver.incus = incus
        with self.assertRaises(RuntimeError):
            driver.prepare(Toolchain(base_image=base_image, pins=(('base_image', pin),)))
        return calls[0][1]

    def test_a_remote_alias_launches_by_remote_fingerprint(self):
        self.assertEqual(self.launched('images:ubuntu/26.04', 'abc'), 'images:abc')

    def test_a_local_alias_launches_by_bare_fingerprint(self):
        self.assertEqual(self.launched('ubuntu-local', 'abc'), 'abc')

    def test_no_incus_fails_the_lookup_at_once(self):
        driver = IncusDriver(root=tempfile.gettempdir())
        with mock.patch('shutil.which', return_value=None):
            with self.assertRaises(Exception):
                driver.image_fingerprint('images:x')


class Sweeps(Scratch):
    """gc with every golden pinned: recipes, not fingerprints, are what clients name."""

    def setUp(self):
        super().setUp()
        self.ledger = Ledger(self.paths.ledger)
        self.addCleanup(self.ledger.close)

    def attempt(self, run_id, spec, at, state='finished'):
        directory = self.paths.attempt(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'toolchain.json').write_text(json.dumps(spec))
        self.ledger.claim('req-' + run_id, run_id, repo='demo', job='suite',
                          input_id='i' + run_id, source_path='/src', argv=['node'],
                          env={}, cwd='/work', outputs=[], size_class='large')
        self.ledger.update(run_id, state=state)
        self.ledger.db.execute('UPDATE attempts SET created=? WHERE run_id=?', (at, run_id))
        self.ledger.db.commit()

    def driver(self, names):
        from pandora.tests.test_worker import FakeDriver
        return FakeDriver([{'name': name, 'state': 'STOPPED', 'created': ''}
                           for name in names])

    def pinned(self, image):
        return dict(RECIPE, source_id='', pins={'base_image': image})

    def test_a_named_recipe_protects_its_newest_golden_only(self):
        specs = [self.pinned(c * 64) for c in 'abc']
        for index, spec in enumerate(specs):
            self.attempt('r%d' % index, spec, 100.0 * (index + 1))
        names = [name_of(spec) for spec in specs]
        driver = self.driver(names)
        recipe = runner.toolchain_of(dict(RECIPE, source_id='')).fingerprint()
        receipt = gc.sweep(self.root, driver, keep=0,
                           protect=gc.parse_protect([recipe + '=demo']),
                           enrolled={('demo', 'recipe:' + recipe)}, repos={'demo'})
        self.assertEqual(sorted(driver.destroyed), sorted(names[:2]))
        whys = {item['name']: item['why'] for item in receipt['kept']}
        self.assertEqual(whys[names[2]], 'named by demo pandora.toml')

    def test_goldens_of_one_recipe_without_a_source_id_are_one_family(self):
        specs = [self.pinned(c * 64) for c in 'ab']
        for index, spec in enumerate(specs):
            self.attempt('r%d' % index, spec, 100.0 * (index + 1))
        driver = self.driver([name_of(spec) for spec in specs])
        gc.sweep(self.root, driver, keep=1)
        self.assertEqual(driver.destroyed, [name_of(specs[0])])

    def test_a_queued_unpinned_attempt_keeps_every_golden_of_its_recipe(self):
        specs = [self.pinned(c * 64) for c in 'ab']
        for index, spec in enumerate(specs):
            self.attempt('r%d' % index, spec, 100.0 * (index + 1))
        self.attempt('rq', dict(RECIPE, source_id=''), 300.0, state='queued')
        driver = self.driver([name_of(spec) for spec in specs])
        receipt = gc.sweep(self.root, driver, keep=0)
        self.assertEqual(driver.destroyed, [])
        self.assertIn('not been pinned', receipt['kept'][0]['why'])

    def test_the_index_carries_the_recipe(self):
        spec = self.pinned('a' * 64)
        self.attempt('r1', spec, 100.0)
        [row] = goldens.index(self.paths, self.driver([name_of(spec)]))
        self.assertEqual(row['recipe'], runner.toolchain_of(dict(RECIPE, source_id=''))
                         .fingerprint())
        self.assertNotEqual(row['recipe'], row['fingerprint'])
        self.assertTrue(row['pinned'])


class Canary(Scratch):
    def setUp(self):
        super().setUp()
        self.ledger = Ledger(self.paths.ledger)
        self.addCleanup(self.ledger.close)

    def test_with_a_source_the_canary_pins_like_a_routed_run(self):
        (self.source / 'uv.lock').write_text('x')
        driver = Images('a' * 64)
        spec, src = canary.pinned_target(self.paths, driver, RECIPE, str(self.source))
        attempt = self.paths.attempt('r1')
        attempt.mkdir()
        (attempt / 'toolchain.json').write_text(json.dumps(RECIPE))
        routed = runner.settle_toolchain(self.paths, 'r1', str(self.source), driver)
        self.assertEqual(name_of(spec), name_of(routed))
        self.assertEqual(src, str(self.source))

    def test_without_a_source_the_canary_proves_the_newest_built_golden(self):
        older = dict(RECIPE, pins={'base_image': 'a' * 64})
        newer = dict(RECIPE, pins={'base_image': 'b' * 64})
        gone = dict(RECIPE, pins={'base_image': 'c' * 64})
        for run_id, spec, at in (('r1', older, 100.0), ('r2', newer, 200.0),
                                 ('r3', gone, 300.0)):
            directory = self.paths.attempt(run_id)
            directory.mkdir()
            (directory / 'toolchain.json').write_text(json.dumps(spec))
            self.ledger.claim('q' + run_id, run_id, repo='demo', job='suite',
                              input_id='i', source_path='/src', argv=['x'], env={},
                              cwd='.', outputs=[], size_class='small')
            self.ledger.db.execute('UPDATE attempts SET created=? WHERE run_id=?',
                                   (at, run_id))
        self.ledger.db.commit()
        driver = Images(built={name_of(older), name_of(newer)})
        spec, src = canary.pinned_target(self.paths, driver, RECIPE,
                                         str(self.root / 'absent'))
        self.assertEqual(name_of(spec), name_of(newer))
        self.assertIsNone(src)

    def test_without_a_source_or_a_pinned_golden_the_recipe_name_stands(self):
        spec, src = canary.pinned_target(self.paths, Images(), RECIPE, None)
        self.assertEqual(name_of(spec), name_of(RECIPE))


if __name__ == '__main__':
    unittest.main()
