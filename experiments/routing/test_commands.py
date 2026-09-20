import unittest
from commands import classify


class CommandsTests(unittest.TestCase):
    def test_journey_scope(self):
        for prefix in ([], ['run'], ['validate'], ['run', 'validate']):
            for selectors in (
                ['S0-01'],
                ['S0-02'],
                ['S0-01', '--update'],
                ['S0-02', '--fault', 'dropped'],
                ['S0-02', '--fault', 'dropped', '--update'],
            ):
                self.assertEqual(classify([*prefix, 'journey', *selectors])[:2],
                                 ('journey', selectors))
        for args in (
            ['journeys'],
            ['journey'],
            ['journey', 'S0-01', '--update', '--update'],
            ['journey', 'S0-02', '--fault'],
            ['journey', 'S0-02', '--fault', 'other'],
            ['journey', 'S0-02', '--fault', 'dropped', 'extra'],
        ):
            self.assertEqual(classify(args)[0], 'reject')

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
