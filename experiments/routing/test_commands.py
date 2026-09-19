import unittest
from commands import classify


class CommandsTests(unittest.TestCase):
    def test_three_treatments(self):
        direct = ['--filter', '@eichler/borrower-web', 'test:e2e', 'smoke.spec.ts', '--workers=1']
        self.assertEqual(classify(direct, 'normal')[0], 'local')
        blocked = classify(direct, 'block')
        self.assertEqual(blocked[0], 'reject')
        self.assertIn('pnpm test:surface borrower-web smoke.spec.ts', blocked[2])
        self.assertEqual(classify(direct, 'redirect')[:2], ('remote', ['smoke.spec.ts']))

    def test_unsupported_flags_never_silently_change_a_remote_run(self):
        self.assertEqual(classify(['test:surface', 'borrower-web', '--update-snapshots'])[0], 'reject')
        self.assertEqual(classify(['--filter', '@eichler/borrower-web', 'test:e2e', '--ui'], 'redirect')[0], 'reject')
        self.assertEqual(classify(['install', '--frozen-lockfile'], 'block')[0], 'local')


if __name__ == '__main__':
    unittest.main()
