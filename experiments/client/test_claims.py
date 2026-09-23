"""The two-tier classifier: the daemon's real one, the shim's derived index.

``repo_config/`` is copied verbatim from ``poc/ci-import``: ``classify.py``,
``config.py``, ``ci_import.py``, ``examples/eichler.pandora.toml`` and
``fixtures/eichler/.github/workflows/ci.yml``.  Nothing in it was edited.
"""
from pathlib import Path
import sys
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import claims
import enrolment
from harness import Sandbox

TOML = HERE / 'repo_config' / 'examples' / 'eichler.pandora.toml'
ROOT = HERE / 'repo_config' / 'fixtures' / 'eichler'


class ImportedClassifier(unittest.TestCase):
    def setUp(self):
        self.config = claims.load_config(TOML, root=ROOT)
        if self.config is None:
            self.skipTest('the ci-import configuration did not load on this machine')

    def test_the_eichler_configuration_loads(self):
        self.assertEqual(self.config['repo']['name'], 'eichler')
        self.assertEqual(sorted(self.config['jobs'])[:3],
                         ['agent-web', 'browser-integration', 'check'])

    def test_loading_costs_more_than_the_whole_shim_budget(self):
        """The reason the shim reads a derived index instead of this file.

        Measured cold, in a fresh interpreter, because that is what a shim is.
        Warm (imports already cached) it is roughly 10 ms, which is still the
        entire budget for the decision this would be part of.
        """
        import subprocess
        started = time.perf_counter()
        result = subprocess.run(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, %r); import config;'
             'config.load(%r, root=%r)' % (str(HERE / 'repo_config'), str(TOML), str(ROOT))],
            capture_output=True)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(elapsed_ms, 30.0)

    def test_the_derived_index_covers_every_claimed_form(self):
        index = claims.index_from_config(self.config)
        for job in self.config['jobs'].values():
            for form in job['forms']:
                self.assertIn(list(form['prefix']), index, job['id'])

    def test_the_index_agrees_with_the_classifier_on_claimed_commands(self):
        import sys as _sys
        _sys.path.insert(0, str(HERE / 'repo_config'))
        import classify
        marker = enrolment.parse(enrolment.render(
            socket_path='/x/y.sock', repo='eichler',
            claims=claims.index_from_config(self.config), heavy=[],
            strip_prefixes=self.config['matching']['strip_prefixes']))
        cases = ['test:unit', 'journeys', 'journey', 'test:surface', 'check', 'test',
                 'lint', 'dev', 'install', 'format', 'test:ios']
        for name in cases:
            index_says = enrolment.claimed([name], marker)
            classifier_says = classify.classify(self.config, ['pnpm', name])['decision'] != 'local'
            self.assertEqual(index_says, classifier_says, name)

    def test_the_index_agrees_on_the_validate_spellings(self):
        import classify
        marker = enrolment.parse(enrolment.render(
            socket_path='/x/y.sock', repo='eichler',
            claims=claims.index_from_config(self.config), heavy=[],
            strip_prefixes=self.config['matching']['strip_prefixes']))
        for tail in (['validate', 'unit'], ['validate', 'journeys'], ['validate', 'nonsense']):
            index_says = enrolment.claimed(tail, marker)
            classifier_says = classify.classify(self.config, ['pnpm', *tail])['decision'] != 'local'
            self.assertEqual(index_says, classifier_says, tail)

    def test_run_prefix_is_declared_by_the_configuration(self):
        self.assertIn(['run'], self.config['matching']['strip_prefixes'])


class DaemonUsesTheConfiguration(unittest.TestCase):
    def setUp(self):
        if claims.load_config(TOML, root=ROOT) is None:
            self.skipTest('the ci-import configuration did not load on this machine')
        self.box = Sandbox(config={'repo_config': {'toml': str(TOML), 'root': str(ROOT)}})
        self.addCleanup(self.box.close)
        self.box.start()
        self.box.enrol(claims=(('test:unit',), ('journeys',), ('lint',)))

    def test_the_daemon_accepts_what_the_configuration_claims(self):
        self.assertEqual(self.box.pnpm(['test:unit']).stdout, b'hello\n')

    def test_the_daemon_refuses_what_the_configuration_does_not_claim(self):
        """The index may over-claim; the daemon is the authority, and refusing
        before acceptance still leaves the command runnable locally."""
        result = self.box.pnpm(['lint'])
        self.assertEqual(result.stdout, b'REAL lint\n')
        self.assertIn(b'no configured job claims', result.stderr)

    def test_a_refused_argv_shape_still_runs_locally(self):
        result = self.box.pnpm(['journeys', '--ui'])
        self.assertNotIn(b'hello', result.stdout)
        self.assertIn(b'REAL', result.stdout)


class StubClassifier(unittest.TestCase):
    def test_the_stub_claims_the_two_documented_commands(self):
        self.assertEqual(claims.stub_index(), [['test:unit'], ['journeys']])


if __name__ == '__main__':
    unittest.main()
