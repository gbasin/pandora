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


class SweepScratch(Scratch):
    """Attempts in a ledger and a fake pool, for the gc tests."""

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


class Sweeps(SweepScratch):
    """gc with every golden pinned: recipes, not fingerprints, are what clients name."""

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



class SharedAlias(Scratch):
    """The image cache is keyed by alias: one recipe's cold build must not move
    it under another recipe."""

    def recipe(self, label):
        return dict(RECIPE, source_id=label)

    def settle_one(self, spec, driver, source, **kw):
        return pinning.settle(spec, self.root, driver, name_of, source=str(source), **kw)

    def test_a_cold_build_of_one_recipe_leaves_another_warm(self):
        a_src, b_src = self.root / 'src' / 'a', self.root / 'src' / 'b'
        for tree in (a_src, b_src):
            tree.mkdir(parents=True)
            (tree / 'pnpm-lock.yaml').write_text('v1\n')
        driver = Images('a' * 64)
        first_a = self.settle_one(self.recipe('a'), driver, a_src, now=1000.0)
        first_b = self.settle_one(self.recipe('b'), driver, b_src, now=1000.0)
        driver.built.update({name_of(first_a), name_of(first_b)})
        # Upstream publishes a new image, and recipe B bumps its lockfile.
        driver.image = 'b' * 64
        (b_src / 'pnpm-lock.yaml').write_text('v2\n')
        cold_b = self.settle_one(self.recipe('b'), driver, b_src, now=2000.0)
        self.assertEqual(cold_b['pins']['base_image'], 'b' * 64)
        self.assertEqual(pinning.read_cache(self.root)['images:ubuntu/26.04']['fingerprint'],
                         'a' * 64)
        lookups = len(driver.lookups)
        again_a = self.settle_one(self.recipe('a'), driver, a_src, now=3000.0)
        self.assertEqual(name_of(again_a), name_of(first_a))
        self.assertEqual(len(driver.lookups), lookups)

    def test_an_expired_entry_is_replaced_by_the_refresh(self):
        driver = Images('a' * 64)
        pinning.image(self.root, 'images:x', driver.image_fingerprint, now=1000.0)
        driver.image = 'b' * 64
        later = 1000.0 + pinning.IMAGE_TTL + 1
        pinning.image(self.root, 'images:x', driver.image_fingerprint, fresh=True,
                      now=later, share=pinning.SHARE_EXPIRED)
        self.assertEqual(pinning.read_cache(self.root)['images:x']['fingerprint'], 'b' * 64)

    def test_a_fresh_entry_is_kept_by_the_refresh(self):
        driver = Images('a' * 64)
        pinning.image(self.root, 'images:x', driver.image_fingerprint, now=1000.0)
        driver.image = 'b' * 64
        found, _ = pinning.image(self.root, 'images:x', driver.image_fingerprint,
                                 fresh=True, now=1001.0, share=pinning.SHARE_EXPIRED)
        self.assertEqual(found, 'b' * 64)
        self.assertEqual(pinning.read_cache(self.root)['images:x']['fingerprint'], 'a' * 64)

    def test_the_diagnostics_never_write_the_cache(self):
        from pandora.engine import service
        driver = Images('d' * 64)
        driver.warm = lambda name: False
        pins.resolve(dict(RECIPE), self.root, driver, source=str(self.source))
        self.assertEqual(pinning.read_cache(self.root), {})
        out = io.StringIO()
        with mock.patch('pandora.executor.incus.IncusDriver', return_value=driver), \
                mock.patch('sys.stdin', io.StringIO(json.dumps({'worker': RECIPE}))), \
                contextlib.redirect_stdout(out):
            service.main(['--root', str(self.root), 'golden'])
        self.assertTrue(json.loads(out.getvalue())['ok'])
        self.assertEqual(pinning.read_cache(self.root), {})
        self.assertEqual(len(driver.lookups), 2)


class Redirects(Scratch):
    """A recipe that rebuilt from a newer image than the shared entry finds
    that golden again with no lookup, until the shared entry expires."""

    DAY = 86400.0

    def setUp(self):
        super().setUp()
        self.trees = {}
        for label in 'ab':
            tree = self.root / 'src' / label
            tree.mkdir(parents=True)
            (tree / 'pnpm-lock.yaml').write_text('v1\n')
            self.trees[label] = tree
        self.driver = Images('0' * 64)
        self.cold = []

    def run_one(self, label, day):
        """One routed run: settle, then build the golden when it is not warm."""
        spec = pinning.settle(dict(RECIPE, source_id=label), self.root, self.driver, name_of,
                              source=str(self.trees[label]), now=1000.0 + day * self.DAY)
        name = name_of(spec)
        if name not in self.driver.built:
            self.cold.append((label, day))
            self.driver.built.add(name)
        return spec

    def upstream(self, day):
        self.driver.image = '%064x' % (day + 1)

    def test_one_cold_build_until_the_shared_entry_expires(self):
        self.run_one('a', 0)
        self.run_one('b', 0)
        self.assertEqual(self.cold, [('a', 0), ('b', 0)])
        shared = pinning.read_cache(self.root)['images:ubuntu/26.04']['fingerprint']
        # Day 1: B bumps its lockfile and upstream has moved.
        (self.trees['b'] / 'pnpm-lock.yaml').write_text('v2\n')
        self.upstream(1)
        rebuilt = self.run_one('b', 1)
        self.assertEqual(rebuilt['pins']['base_image'], '%064x' % 2)
        self.assertEqual(self.cold[-1], ('b', 1))
        entry = pinning.read_cache(self.root)['images:ubuntu/26.04']
        self.assertEqual(entry['fingerprint'], shared)
        self.assertEqual(len(entry['redirect']), 1)
        # Days 2 to 6: upstream moves daily, and day 4 is an outage.
        for day in range(2, 7):
            self.upstream(day)
            if day == 4:
                self.driver.image = RuntimeError('image server down')
            lookups = len(self.driver.lookups)
            spec_b = self.run_one('b', day)
            spec_a = self.run_one('a', day)
            self.assertEqual(name_of(spec_b), name_of(rebuilt), day)
            self.assertEqual(len(self.driver.lookups), lookups, day)
            self.assertTrue(any('redirected' in line for line in spec_b['pin_notes']))
            self.assertEqual(spec_a['pins']['base_image'], shared)
        self.assertEqual(len(self.cold), 3, self.cold)
        # Day 8: the shared entry has expired. Both recipes move, once each.
        self.upstream(8)
        self.run_one('a', 8)
        self.run_one('b', 8)
        self.upstream(9)
        self.run_one('a', 9)
        self.run_one('b', 9)
        self.assertEqual(self.cold[3:], [('a', 8), ('b', 8)])
        entry = pinning.read_cache(self.root)['images:ubuntu/26.04']
        self.assertEqual(entry['fingerprint'], '%064x' % 9)
        self.assertNotIn('redirect', entry)

    def test_a_redirect_older_than_the_shared_entry_is_void(self):
        self.driver.image = 'a' * 64
        pinning.image(self.root, 'images:x', self.driver.image_fingerprint, now=1000.0)
        pinning.remember_redirect(self.root, 'images:x', 'golden-1', 'b' * 64, 2000.0)
        self.assertEqual(pinning.redirect(self.root, 'images:x', 'golden-1'), 'b' * 64)
        cache = pinning.read_cache(self.root)
        cache['images:x']['at'] = 3000.0
        pinning.cache_file(self.root).write_text(json.dumps(cache))
        self.assertIsNone(pinning.redirect(self.root, 'images:x', 'golden-1'))
        pinning.remember_redirect(self.root, 'images:x', 'golden-2', 'c' * 64, 4000.0)
        self.assertEqual(set(pinning.read_cache(self.root)['images:x']['redirect']),
                         {'golden-2'})

    def test_a_failed_refresh_uses_the_redirect_rather_than_the_shared_image(self):
        self.run_one('b', 0)
        (self.trees['b'] / 'pnpm-lock.yaml').write_text('v2\n')
        self.upstream(1)
        rebuilt = self.run_one('b', 1)
        self.driver.built.discard(name_of(rebuilt))       # gc took it
        self.driver.image = RuntimeError('image server down')
        again = self.run_one('b', 2)
        self.assertEqual(again['pins']['base_image'], rebuilt['pins']['base_image'])

    def test_read_only_callers_follow_the_redirect(self):
        self.run_one('b', 0)
        (self.trees['b'] / 'pnpm-lock.yaml').write_text('v2\n')
        self.upstream(1)
        rebuilt = self.run_one('b', 1)
        self.upstream(2)
        before = json.dumps(pinning.read_cache(self.root), sort_keys=True)
        asked = pinning.settle(dict(RECIPE, source_id='b'), self.root, self.driver, name_of,
                               source=str(self.trees['b']), now=1000.0 + 2 * self.DAY,
                               write=False)
        self.assertEqual(name_of(asked), name_of(rebuilt))
        self.assertEqual(json.dumps(pinning.read_cache(self.root), sort_keys=True), before)


class NegativeCache(Scratch):
    def test_a_failed_lookup_is_not_repeated_for_five_minutes(self):
        driver = Images(RuntimeError('image server down'))
        found, how = pinning.image(self.root, 'images:x', driver.image_fingerprint,
                                   now=1000.0)
        self.assertIsNone(found)
        found, how = pinning.image(self.root, 'images:x', driver.image_fingerprint,
                                   now=1000.0 + pinning.FAILED_TTL - 1)
        self.assertIsNone(found)
        self.assertIn('failed', how)
        self.assertIn('image server down', how)
        self.assertEqual(len(driver.lookups), 1)
        driver.image = 'a' * 64
        found, how = pinning.image(self.root, 'images:x', driver.image_fingerprint,
                                   now=1000.0 + pinning.FAILED_TTL + 1)
        self.assertEqual((found, how), ('a' * 64, 'resolved'))
        self.assertNotIn('failed_at', pinning.read_cache(self.root)['images:x'])

    def test_a_failure_keeps_the_last_answer_for_the_window(self):
        driver = Images('a' * 64)
        pinning.image(self.root, 'images:x', driver.image_fingerprint, now=1000.0)
        driver.image = RuntimeError('down')
        expired = 1000.0 + pinning.IMAGE_TTL + 1
        pinning.image(self.root, 'images:x', driver.image_fingerprint, now=expired)
        found, how = pinning.image(self.root, 'images:x', driver.image_fingerprint,
                                   now=expired + 60)
        self.assertEqual(found, 'a' * 64)
        self.assertTrue(how.startswith('stale: the lookup failed'), how)
        self.assertEqual(len(driver.lookups), 2)

    def test_a_read_only_failure_is_not_recorded(self):
        driver = Images(RuntimeError('down'))
        pinning.image(self.root, 'images:x', driver.image_fingerprint, now=1000.0,
                      share=pinning.SHARE_NEVER)
        self.assertEqual(pinning.read_cache(self.root), {})

    def test_the_cache_holds_a_bounded_number_of_aliases(self):
        driver = Images('a' * 64)
        for index in range(pinning.MAX_ALIASES + 8):
            pinning.image(self.root, 'images:x%d' % index, driver.image_fingerprint,
                          now=1000.0 + index)
        cache = pinning.read_cache(self.root)
        self.assertEqual(len(cache), pinning.MAX_ALIASES)
        self.assertNotIn('images:x0', cache)
        self.assertIn('images:x%d' % (pinning.MAX_ALIASES + 7), cache)

    def test_an_alias_that_looks_like_a_flag_is_never_looked_up(self):
        driver = Images('a' * 64)
        found, how = pinning.image(self.root, '--help', driver.image_fingerprint, now=1.0)
        self.assertIsNone(found)
        self.assertTrue(how.startswith('unresolved'), how)
        self.assertEqual(driver.lookups, [])


class WarmNotExists(Scratch):
    def test_a_golden_without_its_warm_snapshot_counts_as_unbuilt(self):
        first = self.settle(Images('a' * 64))
        driver = Images('b' * 64, built={name_of(first)})
        driver.warm = lambda name: False
        second = self.settle(driver)
        self.assertEqual(second['pins']['base_image'], 'b' * 64)


class LaunchFallback(unittest.TestCase):
    def test_an_unlaunchable_pinned_image_falls_back_to_the_alias(self):
        from pandora.executor.interface import Toolchain
        driver = IncusDriver(root=tempfile.gettempdir())
        calls, lines = [], []

        def incus(*args, **kw):
            calls.append(args)
            if args[0] == 'launch' and args[1] == 'images:gone':
                return 1, '', 'Error: Image not found'
            if args[0] == 'launch':
                raise RuntimeError('stop after the alias launch')
            return 0, '', ''
        driver.exists = lambda name: False
        driver.incus = incus
        with self.assertRaises(RuntimeError):
            driver.prepare(Toolchain(base_image='images:ubuntu/26.04',
                                     pins=(('base_image', 'gone'),)), log=lines.append)
        launches = [args[1] for args in calls if args[0] == 'launch']
        self.assertEqual(launches, ['images:gone', 'images:ubuntu/26.04'])
        self.assertIn(('delete', '-f'), [args[:2] for args in calls])
        self.assertTrue(any('launching the alias' in line for line in lines), lines)


class BuiltFrom(Scratch):
    """A golden launched from its alias says which image it really is."""

    def test_the_alias_fallback_records_the_launched_image(self):
        from pandora.executor.interface import Toolchain
        driver = IncusDriver(root=tempfile.gettempdir())
        calls = []

        def incus(*args, **kw):
            calls.append(args)
            if args[:2] == ('launch', 'images:gone'):
                return 1, '', 'Error: Image not found'
            if args[:4] == ('config', 'get', args[2], 'volatile.base_image'):
                return 0, 'f' * 64 + '\n', ''
            return 0, '', ''
        driver.exists = lambda name: False
        driver.incus = incus

        def stop(name, timeout=120):
            raise RuntimeError('stop after the launch')
        driver.wait_ready = stop
        with self.assertRaises(RuntimeError):
            driver.prepare(Toolchain(base_image='images:ubuntu/26.04',
                                     pins=(('base_image', 'gone'),)), log=lambda text: None)
        sets = [args for args in calls if args[:2] == ('config', 'set')]
        self.assertEqual(len(sets), 1)
        self.assertEqual(sets[0][3:], ('user.pandora.built_from', 'f' * 64))

    def test_a_reused_golden_reports_what_it_was_built_from(self):
        from pandora.executor.interface import Toolchain
        driver = IncusDriver(root=tempfile.gettempdir())

        def incus(*args, **kw):
            if args[:2] == ('config', 'get') and args[3] == 'user.pandora.built_from':
                return 0, 'f' * 64 + '\n', ''
            return 0, '', ''
        driver.exists = lambda name: True
        driver.warm = lambda name: True
        driver.volume_bytes = lambda name: 0
        driver.incus = incus
        golden = driver.prepare(Toolchain(base_image='images:x', pins=(('base_image', 'a'),)))
        self.assertTrue(golden.reused)
        self.assertEqual(golden.built_from, 'f' * 64)

    def test_the_verdict_pins_carry_built_from_when_it_differs(self):
        from pandora.executor.interface import Golden
        attempt = self.paths.attempt('r1')
        attempt.mkdir(parents=True)
        spec = dict(RECIPE, pins={'base_image': 'a' * 64})
        (attempt / 'toolchain.json').write_text(json.dumps(spec))
        golden = Golden(name=name_of(spec), fingerprint='x', snapshot='warm')
        runner.record_built_from(self.paths, 'r1', spec, golden)
        fingerprint, found = runner.golden_and_pins(self.paths, 'r1')
        self.assertNotIn('built_from', found)
        runner.record_built_from(self.paths, 'r1', spec,
                                 Golden(name=golden.name, fingerprint='x', snapshot='warm',
                                        built_from='a' * 64))
        self.assertNotIn('built_from', runner.golden_and_pins(self.paths, 'r1')[1])
        lines = []
        runner.record_built_from(self.paths, 'r1', spec,
                                 Golden(name=golden.name, fingerprint='x', snapshot='warm',
                                        built_from='f' * 64), lines.append)
        again, found = runner.golden_and_pins(self.paths, 'r1')
        self.assertEqual(found['built_from'], 'f' * 64)
        self.assertEqual(found['image'], 'a' * 64)
        self.assertEqual(again, fingerprint)            # the name does not move
        self.assertIn('not its pinned image', lines[0])


class Lockfiles(Scratch):
    def test_a_symlink_outside_the_tree_is_refused(self):
        outside = Path(self.tmp.name) / 'host.lock'
        outside.write_text('host secret\n')
        (self.source / 'uv.lock').symlink_to(outside)
        notes = []
        self.assertEqual(pinning.lockfiles(self.source, notes), {})
        self.assertIn('refused', notes[0])
        spec = self.settle(Images())
        self.assertNotIn('lockfile:uv.lock', spec['pins'])
        self.assertTrue(any('refused' in line for line in spec['pin_notes']))

    def test_a_symlink_inside_the_tree_is_hashed(self):
        (self.source / 'locks').mkdir()
        (self.source / 'locks' / 'uv.lock').write_text('inside\n')
        (self.source / 'uv.lock').symlink_to(self.source / 'locks' / 'uv.lock')
        self.assertEqual(pinning.lockfiles(self.source),
                         {'uv.lock': hashlib.sha256(b'inside\n').hexdigest()})

    def test_a_large_lockfile_hashes_in_chunks_to_the_same_digest(self):
        data = b'x' * (pinning.CHUNK * 2 + 17)
        (self.source / 'yarn.lock').write_bytes(data)
        self.assertEqual(pinning.lockfiles(self.source)['yarn.lock'],
                         hashlib.sha256(data).hexdigest())


class GoldenVerb(Scratch):
    def ask(self, text):
        from pandora.engine import service
        driver = Images('d' * 64)
        driver.warm = lambda name: False
        out = io.StringIO()
        with mock.patch('pandora.executor.incus.IncusDriver', return_value=driver), \
                mock.patch('sys.stdin', io.StringIO(text)), \
                contextlib.redirect_stdout(out):
            service.main(['--root', str(self.root), 'golden'])
        return json.loads(out.getvalue()), driver

    def assertBad(self, text, words):
        answer, driver = self.ask(text)
        self.assertEqual((answer['ok'], answer['code']), (False, 'bad-request'), answer)
        self.assertIn(words, answer['detail'])
        self.assertEqual(driver.lookups, [])

    def test_malformed_requests_get_a_clear_error(self):
        self.assertBad('not json', '')
        self.assertBad(json.dumps([1]), 'stdin must be')
        self.assertBad(json.dumps({'worker': RECIPE, 'extra': 1}), 'extra')
        self.assertBad(json.dumps({'worker': dict(RECIPE, surprise=1)}), 'surprise')
        self.assertBad(json.dumps({'worker': dict(RECIPE, packages='git')}), 'packages')
        self.assertBad(json.dumps({'worker': dict(RECIPE, base_image='--help')}), '"-"')
        self.assertBad(json.dumps({'worker': RECIPE, 'lockfiles': {'uv.lock': 'nothex'}}),
                       'uv.lock')
        self.assertBad(json.dumps({'worker': RECIPE, 'lockfiles': {'../x': 'a' * 64}}),
                       '../x')

    def test_pins_in_the_request_are_ignored(self):
        answer, _ = self.ask(json.dumps({'worker': dict(RECIPE, pins={'base_image': 'e' * 64},
                                                        pin_notes=['x'])}))
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(answer['golden_pins']['image'], 'd' * 64)


class SubmitStripsPins(unittest.TestCase):
    """A client cannot choose the golden, or the `golden_pins` a verdict signs."""

    def setUp(self):
        from pandora.tests.test_engine import SourceConfinementTest
        SourceConfinementTest.setUp(self)
        self.submit_one = lambda source: SourceConfinementTest.submit(self, source)

    def test_pins_and_pin_notes_are_dropped_at_submit(self):
        import pandora.tests.test_engine as engine_tests
        source = self.root / 'src' / 'demo' / 'input-a'
        source.mkdir(parents=True)
        forged = dict(engine_tests.PLAN['worker'], pins={'base_image': 'f' * 64},
                      pin_notes=['chosen by the client'], built_from='e' * 64)
        with mock.patch.dict(engine_tests.PLAN, worker=forged):
            answer = self.submit_one(str(source))
        self.assertTrue(answer['ok'], answer)
        written = json.loads((runner.Paths(self.root).attempt(answer['run_id'])
                              / 'toolchain.json').read_text())
        self.assertNotIn('pins', written)
        self.assertNotIn('pin_notes', written)
        self.assertNotIn('built_from', written)
        self.assertEqual(written['base_image'], forged['base_image'])


class GcOnePass(SweepScratch):
    def test_an_attempt_pinned_between_the_old_two_walks_keeps_its_golden(self):
        """The old sweep read names, then recipes. An attempt unpinned in the
        first read and pinned in the second held neither."""
        pinned = self.pinned('a' * 64)
        unpinned = dict(RECIPE, source_id='')
        self.attempt('r0', pinned, 100.0)
        self.attempt('rq', unpinned, 300.0, state='running')
        driver = self.driver([name_of(pinned)])
        real = goldens.attempts
        reads = []

        def racing(paths):
            # The supervisor settles `rq` right after the sweep's first read.
            rows = real(paths)
            reads.append(1)
            if len(reads) > 1:
                rows['rq']['toolchain'] = pinned
            return rows
        with mock.patch.object(goldens, 'attempts', racing):
            # A dry run has no pre-delete re-check to hide the race behind.
            receipt = gc.sweep(self.root, driver, keep=0, dry_run=True)
        self.assertEqual(receipt['removed'], [], receipt)
        self.assertIn('not been pinned', receipt['kept'][0]['why'])

    def test_a_run_submitted_between_the_two_reads_is_protected(self):
        """A submit claims the row, then writes `toolchain.json`. Read in the
        other order, a submit between the reads looked like a dead attempt."""
        unpinned = dict(RECIPE, source_id='')
        real = goldens.toolchains

        def submit_then_read(paths):
            self.attempt('rq', unpinned, 300.0, state='queued')
            return real(paths)
        with mock.patch.object(goldens, 'toolchains', submit_then_read):
            names, recipes = goldens.live_state(self.paths)
        self.assertEqual(recipes, {goldens.recipe_of(unpinned)})

    def test_live_state_reads_the_attempts_once(self):
        self.attempt('rq', dict(RECIPE, source_id=''), 300.0, state='queued')
        with mock.patch.object(goldens, 'attempts', wraps=goldens.attempts) as spy:
            names, recipes = goldens.live_state(self.paths)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(recipes, {goldens.recipe_of(dict(RECIPE, source_id=''))})


if __name__ == '__main__':
    unittest.main()
