"""Agreement between the welded classifier and the config-driven one.

``experiments/routing/commands.py`` is the shipped v0.1.1 boundary.  Every case
in ``CASES`` runs through both and must produce the same decision.  ``DIVERGENT``
is the price of review item 3: the configuration no longer re-implements
eichler's argument grammar, so eight spellings that v0.1.1 refused locally are
now forwarded for the repository's own runner to refuse.  Each one is pinned
here with its reason, so the list cannot grow by accident.
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXPERIMENTS = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(EXPERIMENTS / 'routing'))
sys.path.insert(0, str(EXPERIMENTS / 'warm'))

from classify import classify
from config import load
import commands as welded

EICHLER = load(HERE / 'examples' / 'eichler.pandora.toml', root=HERE / 'fixtures' / 'eichler')
REMOTE = {'validation', 'journey', 'suite-run', 'remote'}

# Every accepted, rejected and passthrough form the v0.1.1 contract names, plus
# `pnpm run` spellings and near misses.
CASES = [
    # broad validation suites
    ['test:unit'],
    ['validate', 'unit'],
    ['run', 'test:unit'],
    ['run', 'validate', 'unit'],
    ['test:tools'],
    ['validate', 'tools'],
    ['test:tools', 'tools/check.test.mjs'],
    ['validate', 'tools', 'tools/check.test.mjs'],
    ['test:unit', 'apps/api/src/x.test.ts'],
    ['test'],
    ['run', 'test'],
    ['validate', 'full'],
    ['test', 'apps/api/src/x.test.ts'],
    ['validate', 'agent-web'],
    ['test:agent-web'],
    ['test:employee-browser'],
    ['validate', 'employee-browser'],
    ['test:employee-browser', '--headed'],
    ['test:browser-integration'],
    ['validate', 'browser-integration', 'one.spec.ts'],
    ['test:mockup-browser'],
    ['validate', 'mockup-browser'],
    # postgres positional and conditional flag
    ['test:postgres', 'api'],
    ['test:postgres', 'scenarios'],
    ['validate', 'postgres', 'api', '--foundation-only'],
    ['validate', 'postgres'],
    ['test:postgres'],
    # focused journeys
    ['journey', 'S0-01'],
    ['run', 'journey', 'S0-02', '--fault', 'dropped'],
    ['validate', 'journey', 'S0-02', '--fault', 'dropped', '--update'],
    ['journey', 'S0-02', '--update', '--fault', 'dropped'],
    ['run', 'validate', 'journey', 'S0-01', '--update'],
    ['journey'],
    ['journey', 'S0-01', '--update', '--update'],
    ['journey', 'S0-02', '--fault'],
    # journey catalog
    ['journeys'],
    ['journeys', '--update', '--keep-going'],
    ['journeys', '--keep-going', '--update'],
    ['run', 'validate', 'journeys', '--keep-going'],
    ['journeys', '--keep-going', '--keep-going'],
    ['journeys', '--update', '--update'],
    ['journeys', 'S0-01'],
    ['validate', 'journeys', '--fault', 'dropped'],
    # browser surfaces
    ['test:surface', 'borrower-web'],
    ['test:surface', 'borrower-web', 'smoke.spec.ts', '--grep', 'income'],
    ['validate', 'surface', 'desk', 'pipeline.spec.ts', '--grep', 'review'],
    ['test:surface', 'desk', '--grep', 'assign loan', 'tasks.spec.ts'],
    ['test:surface', 'desk', 'pipeline.spec.ts', '--grep', 'review', '--keep-going'],
    ['test:surface', 'desk', '--grep', '--keep-going'],
    ['run', 'test:surface', 'desk'],
    ['test:surface'],
    ['validate', 'surface'],
    ['test:surface', 'desk', '--ui'],
    ['test:surface', 'borrower-web', '--update-snapshots'],
    ['test:surface', 'desk', '--grep'],
    ['test:surface', 'desk', '--keep-going', '--keep-going'],
    # ordinary local pnpm work
    ['install', '--frozen-lockfile'],
    ['lint'],
    ['build'],
    ['typecheck'],
    ['unit'],
    ['full'],
    ['postgres', 'api'],
    ['agent-web'],
    ['validate'],
    ['validate', 'test:unit'],
    ['run', 'validate', 'test:tools'],
    ['--filter', '@eichler/borrower-web', 'test:e2e', 'smoke.spec.ts', '--workers=1'],
    ['exec', 'vitest', 'run'],
]

# The cost of not re-implementing eichler's CLI: v0.1.1 knew these were wrong
# without starting anything.  The configuration claims the form, forwards the
# arguments, and the repository's runner produces the error -- after a container
# has started.  Each entry names the knowledge Pandora gave up.
DIVERGENT = [
    (['test:postgres', 'scenarios', '--foundation-only'],
     'which positional a conditional flag belongs to'),
    (['validate', 'postgres', 'api', '--foundation-only', 'extra'],
     'how many arguments a suite accepts'),
    (['journey', 'S0-02', '--fault', 'other'], 'the closed value list of --fault'),
    (['journey', 'S0-02', '--fault', 'dropped', 'extra'], 'how many arguments a journey accepts'),
    (['journey', 'S9-01'], 'the journey-id pattern (?:S[0-6]|SX)-[0-9]{2}'),
    (['test:surface', 'ops', 'smoke.spec.ts'], 'the app enum borrower-web|desk'),
    (['validate', 'surface', 'ops', 'smoke.spec.ts'], 'the app enum borrower-web|desk'),
    (['test:surface', 'borrower-web', '--grep', 'first', '--grep', 'second'],
     'that --grep may appear at most once'),
]

# Forms the configuration deliberately routes that v0.1.1 leaves local.
NEW_JOBS = [['check'], ['validate', 'check'], ['test:native-unit'], ['validate', 'native-unit']]
# Focused forms of the new jobs must still fall back, like their siblings.
NEW_JOB_LOCAL = [['test:native-unit', 'apps/agent/src/x.test.ts']]


def decision(action):
    return 'remote' if action in REMOTE else action


class ParityTests(unittest.TestCase):
    def test_case_table_is_large_enough_to_be_evidence(self):
        self.assertGreaterEqual(len(CASES) + len(DIVERGENT), 70)

    def test_welded_and_configured_classifiers_agree(self):
        for argv in CASES:
            with self.subTest(argv=argv):
                expected = decision(welded.classify(argv)[0])
                self.assertEqual(classify(EICHLER, argv)['decision'], expected)

    def test_every_refusal_carries_a_usable_message(self):
        for argv in CASES:
            result = classify(EICHLER, argv)
            if result['decision'] != 'reject':
                continue
            with self.subTest(argv=argv):
                self.assertTrue(result['message'].endswith('No validation started.'))
                self.assertGreater(len(result['message']), len('No validation started.') + 10)

    def test_forwarded_surface_arguments_are_byte_identical(self):
        """What the repository's runner receives is the part agents observe."""
        for argv in CASES:
            action, selectors, _ = welded.classify(argv)
            if action != 'remote':
                continue
            with self.subTest(argv=argv):
                plan = classify(EICHLER, argv)['plan']
                self.assertEqual(plan['args'], [welded.selected_surface(argv), *selectors])

    def test_journey_and_suite_options_are_preserved(self):
        for argv in CASES:
            action, _, _ = welded.classify(argv)
            if action not in {'journey', 'suite-run'}:
                continue
            with self.subTest(argv=argv):
                plan = classify(EICHLER, argv)['plan']
                if action == 'suite-run':
                    expected = welded.suite_request(argv, 4)
                    self.assertEqual(plan['options']['update'], expected['update'])
                    self.assertEqual(plan['options']['keep_going'], expected['keep_going'])
                    self.assertEqual(plan['shard_count'], expected['shard_count'])
                else:
                    from journey import journey_config
                    expected = journey_config({'selectors': welded.classify(argv)[1]})
                    self.assertIn(expected['id'], plan['args'])
                    self.assertEqual(plan['options']['update'], expected['update'])
                    if expected['fault']:
                        self.assertEqual(plan['args'][-2:], ['--fault', expected['fault']])

    def test_validation_suites_forward_the_same_arguments(self):
        for argv in CASES:
            if welded.classify(argv)[0] != 'validation':
                continue
            with self.subTest(argv=argv):
                request = welded.validation_request(argv)
                plan = classify(EICHLER, argv)['plan']
                self.assertEqual(plan['job'], request['suite'])
                self.assertEqual(plan['args'], request['args'])


class DivergenceTests(unittest.TestCase):
    """Differences that exist on purpose, recorded so they cannot drift silently."""

    def test_the_shrunk_argv_language_forwards_what_it_used_to_judge(self):
        for argv, knowledge in DIVERGENT:
            with self.subTest(argv=argv, gave_up=knowledge):
                self.assertEqual(welded.classify(argv)[0], 'reject')
                self.assertEqual(classify(EICHLER, argv)['decision'], 'remote')

    def test_the_divergence_list_is_exactly_this_long(self):
        found = [argv for argv in (case for case, _ in DIVERGENT)]
        self.assertEqual(len(found), 8)

    def test_new_jobs_route_where_v0_1_1_fell_back(self):
        for argv in NEW_JOBS:
            with self.subTest(argv=argv):
                self.assertEqual(welded.classify(argv)[0], 'local')
                self.assertEqual(classify(EICHLER, argv)['decision'], 'remote')
        for argv in NEW_JOB_LOCAL:
            with self.subTest(argv=argv):
                self.assertEqual(classify(EICHLER, argv)['decision'], 'local')

    def test_subdirectory_reroots_instead_of_exiting_64(self):
        # route.py refuses any routed command outside the repository root.
        # The configured classifier re-roots file arguments instead.
        for argv in (['test:unit'], ['journeys'], ['test:surface', 'desk', 'e2e/a.spec.ts']):
            with self.subTest(argv=argv):
                result = classify(EICHLER, argv, cwd='apps/desk')
                self.assertEqual(result['decision'], 'remote')
        self.assertEqual(
            classify(EICHLER, ['test:surface', 'desk', 'e2e/a.spec.ts'],
                     cwd='apps/desk')['plan']['args'],
            ['desk', 'apps/desk/e2e/a.spec.ts'])

    def test_a_selector_with_no_separator_or_suffix_is_not_rerooted(self):
        """The honest limit of the re-rooting heuristic, now that nothing is typed."""
        self.assertEqual(
            classify(EICHLER, ['test:surface', 'desk', 'smoke'], cwd='apps/desk')['plan']['args'],
            ['desk', 'smoke'])

    def test_direct_package_treatments_are_not_part_of_the_contract(self):
        # PANDORA_TREATMENT block/redirect were a trial instrument, not a
        # routed form; the configuration has no equivalent and leaves it local.
        direct = ['--filter', '@eichler/borrower-web', 'test:e2e', 'smoke.spec.ts']
        self.assertEqual(welded.classify(direct, 'redirect')[0], 'remote')
        self.assertEqual(welded.classify(direct, 'normal')[0], 'local')
        self.assertEqual(classify(EICHLER, direct)['decision'], 'local')


if __name__ == '__main__':
    unittest.main()
