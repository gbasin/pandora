import unittest
from commands import classify, suite_request, surface_suite_request


class CommandsTests(unittest.TestCase):
    def test_journey_scope(self):
        for prefix in ([], ['run'], ['validate'], ['run', 'validate']):
            for selectors in (
                ['S0-01'],
                ['S0-02'],
                ['S0-01', '--update'],
                ['S0-02', '--fault', 'dropped'],
                ['S0-02', '--fault', 'dropped', '--update'],
                ['S0-02', '--update', '--fault', 'dropped'],
            ):
                self.assertEqual(classify([*prefix, 'journey', *selectors])[:2],
                                 ('journey', selectors))
        for args in (
            ['journey'],
            ['journey', 'S0-01', '--update', '--update'],
            ['journey', 'S0-02', '--fault'],
            ['journey', 'S0-02', '--fault', 'other'],
            ['journey', 'S0-02', '--fault', 'dropped', 'extra'],
        ):
            self.assertEqual(classify(args)[0], 'reject')

    def test_catalog_journeys_route_a_bounded_suite_request(self):
        for prefix in ([], ['run'], ['validate'], ['run', 'validate']):
            for options, keep_going, update in (([], False, False), (['--keep-going'], True, False), (['--update'], False, True), (['--update', '--keep-going'], True, True), (['--keep-going', '--update'], True, True)):
                argv = [*prefix, 'journeys', *options]
                self.assertEqual(classify(argv)[:2], ('suite-run', []))
                self.assertEqual(suite_request(argv, 7), {
                    'action': 'run', 'shard_count': 7, 'selection': None,
                    'keep_going': keep_going, 'update': update,
                })

    def test_catalog_journeys_rejects_unroutable_options_with_a_focused_update_alternative(self):
        for argv in (
            ['journeys', '--keep-going', '--keep-going'],
            ['journeys', 'S0-01'],
            ['journeys', '--update', '--update'],
            ['run', 'journeys', '--keep-going', '--keep-going'],
            ['validate', 'journeys', '--fault', 'dropped'],
        ):
            action, _, message = classify(argv)
            self.assertEqual(action, 'reject')
            self.assertIn('No validation started.', message)

    def test_surface_preserves_exact_file_and_grep_argv_for_each_app(self):
        for command, expected in (
            (['test:surface', 'borrower-web', 'smoke.spec.ts', '--grep', 'income'],
             ['smoke.spec.ts', '--grep', 'income']),
            (['test:surface', 'desk', '--grep', 'assign loan', 'tasks.spec.ts'],
             ['--grep', 'assign loan', 'tasks.spec.ts']),
            (['validate', 'surface', 'desk', 'pipeline.spec.ts', '--grep', 'review'],
             ['pipeline.spec.ts', '--grep', 'review']),
        ):
            self.assertEqual(classify(command)[:2], ('remote', expected))

    def test_surface_suite_request_keeps_public_selectors_and_accepts_keep_going(self):
        command = ['test:surface', 'desk', 'pipeline.spec.ts', '--grep', 'review', '--keep-going']
        self.assertEqual(classify(command)[:2], ('remote', ['pipeline.spec.ts', '--grep', 'review']))
        self.assertEqual(surface_suite_request(command, 6), {
            'action': 'run', 'app': 'desk', 'selectors': ['pipeline.spec.ts', '--grep', 'review'],
            'shard_count': 6, 'keep_going': True,
        })
        with self.assertRaisesRegex(ValueError, 'at most one'):
            surface_suite_request(command + ['--keep-going'], 6)

    def test_surface_keep_going_does_not_rewrite_a_grep_pattern(self):
        command = ['test:surface', 'desk', '--grep', '--keep-going']
        self.assertEqual(classify(command)[:2], ('remote', ['--grep', '--keep-going']))
        self.assertEqual(surface_suite_request(command, 4), {
            'action': 'run', 'app': 'desk', 'selectors': ['--grep', '--keep-going'],
            'shard_count': 4, 'keep_going': False,
        })
        self.assertEqual(classify(['test:surface', 'desk', '--keep-going', '--keep-going'])[0], 'reject')

    def test_three_treatments(self):
        direct = ['--filter', '@eichler/borrower-web', 'test:e2e', 'smoke.spec.ts', '--workers=1']
        self.assertEqual(classify(direct, 'normal')[0], 'local')
        blocked = classify(direct, 'block')
        self.assertEqual(blocked[0], 'reject')
        self.assertIn('pnpm test:surface borrower-web smoke.spec.ts', blocked[2])
        self.assertEqual(classify(direct, 'redirect')[:2], ('remote', ['smoke.spec.ts']))

    def test_unsupported_flags_never_silently_change_a_remote_run(self):
        for command in (
            ['test:surface', 'borrower-web', '--update-snapshots'],
            ['test:surface', 'desk', '--ui'],
            ['test:surface', 'ops', 'smoke.spec.ts'],
            ['validate', 'surface', 'ops', 'smoke.spec.ts'],
            ['test:surface', 'desk', '--grep'],
            ['test:surface', 'borrower-web', '--grep', 'first', '--grep', 'second'],
        ):
            self.assertEqual(classify(command)[0], 'reject')
        self.assertEqual(classify(['--filter', '@eichler/borrower-web', 'test:e2e', '--ui'], 'redirect')[0], 'reject')
        self.assertEqual(classify(['install', '--frozen-lockfile'], 'block')[0], 'local')


if __name__ == '__main__':
    unittest.main()
