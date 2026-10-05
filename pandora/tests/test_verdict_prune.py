"""scripts/verdict-prune.sh: which verdict refs a weekly prune deletes.

Each case builds a bare `origin` holding verdict refs made the way the client
makes them (a parentless commit dated `@1 +0000`), clones it, and runs the
script there with a fixed clock. Age comes from `finished` in payload.json.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'verdict-prune.sh'
NOW = 1_800_000_000
DAY = 86400


def git(repo, *args, stdin=None, env=None):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, input=stdin,
                          capture_output=True, env=env).stdout.decode().strip()


@unittest.skipUnless(shutil.which('bash') and shutil.which('git'), 'bash and git required')
class VerdictPruneTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='pandora-prune-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.origin = self.root / 'origin.git'
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(self.origin)],
                       check=True)
        self.dev = self.root / 'dev'
        subprocess.run(['git', 'clone', '-q', str(self.origin), str(self.dev)],
                       check=True, capture_output=True)
        git(self.dev, 'config', 'user.email', 'test@example.com')
        git(self.dev, 'config', 'user.name', 'Test')
        (self.dev / 'README').write_text('base\n')
        git(self.dev, 'add', '-A')
        git(self.dev, 'commit', '-qm', 'base')
        git(self.dev, 'push', '-q', 'origin', 'main')
        self.serial = 0

    def publish(self, job='suite', finished=None, payload=None, files=None, ref=None):
        """Push one verdict-shaped commit; return its ref.

        `finished` is seconds before NOW; `payload` replaces the payload bytes;
        `files` replaces the whole file set. A dict value in `files` is a
        directory of that name.
        """
        self.serial += 1
        tree = '%040x' % self.serial
        if payload is None:
            fields = {'job': job, 'kind': 'pandora-verdict', 'outcome': 'passed',
                      'tree': tree, 'v': 1}
            if finished is not None:
                fields['finished'] = NOW - finished + 0.25
            payload = json.dumps(fields, sort_keys=True, separators=(',', ':'))
        if files is None:
            files = {'payload.json': payload, 'signer': 'key\n', 'verdict.sig': 'sig\n'}
        made = self.mktree(files)
        env = dict(os.environ, GIT_AUTHOR_NAME='pandora', GIT_AUTHOR_EMAIL='pandora@localhost',
                   GIT_AUTHOR_DATE='@1 +0000', GIT_COMMITTER_NAME='pandora',
                   GIT_COMMITTER_EMAIL='pandora@localhost', GIT_COMMITTER_DATE='@1 +0000')
        commit = git(self.dev, 'commit-tree', made, '-m', 'pandora verdict %s %s' % (tree, job),
                     env=env)
        ref = ref or 'refs/pandora/verdicts/%s/%s' % (tree, job)
        git(self.dev, 'push', '-q', 'origin', '+%s:%s' % (commit, ref))
        return ref

    def mktree(self, files):
        lines = []
        for name, body in sorted(files.items()):
            if isinstance(body, dict):
                lines.append('040000 tree %s\t%s\n' % (self.mktree(body), name))
            else:
                blob = git(self.dev, 'hash-object', '-w', '--stdin', stdin=body.encode())
                lines.append('100644 blob %s\t%s\n' % (blob, name))
        return git(self.dev, 'mktree', stdin=''.join(lines).encode())

    def remote_oid(self, ref):
        return git(self.origin, 'rev-parse', ref)

    def remote_refs(self):
        out = git(self.origin, 'for-each-ref', '--format=%(refname)')
        return set(out.splitlines())

    def prune(self, *args, path=None):
        ci = self.root / 'ci'
        if not ci.exists():
            subprocess.run(['git', 'clone', '-q', '--depth', '1', 'file://%s' % self.origin,
                            str(ci)], check=True, capture_output=True)
        env = dict(os.environ)
        env.pop('GITHUB_ACTIONS', None)
        if path is not None:
            env['PATH'] = '%s%s%s' % (path, os.pathsep, env['PATH'])
        return subprocess.run(['bash', str(SCRIPT), '--now', str(NOW), *args], cwd=ci,
                              env=env, capture_output=True, text=True, timeout=300)

    def test_refs_older_than_the_threshold_are_deleted_in_batches(self):
        old = [self.publish(finished=d * DAY) for d in (31, 45, 400)]
        young = [self.publish(finished=d * DAY) for d in (0, 5, 29)]
        edge = self.publish(finished=30 * DAY)
        done = self.prune('--batch', '2')
        self.assertEqual(done.returncode, 0, done.stderr)
        left = self.remote_refs()
        for ref in old:
            self.assertNotIn(ref, left)
        for ref in young + [edge, 'refs/heads/main']:
            self.assertIn(ref, left)
        self.assertIn('7 refs, 3 older than 30 days, 3 deleted, 4 kept, 0 changed, 0 unreadable',
                      done.stdout)

    def test_the_threshold_is_an_input(self):
        five = self.publish(finished=5 * DAY)
        one = self.publish(finished=1 * DAY)
        done = self.prune('--max-age-days', '3')
        self.assertEqual(done.returncode, 0, done.stderr)
        left = self.remote_refs()
        self.assertNotIn(five, left)
        self.assertIn(one, left)

    def test_a_verdict_without_a_readable_finished_time_is_kept(self):
        kept = [self.publish(finished=None),
                self.publish(payload='not json'),
                self.publish(payload=json.dumps({'finished': 'yesterday'})),
                self.publish(payload=json.dumps({'finished': True})),
                self.publish(files={'signer': 'key\n'})]
        old = self.publish(finished=90 * DAY)
        done = self.prune()
        self.assertEqual(done.returncode, 0, done.stderr)
        left = self.remote_refs()
        self.assertNotIn(old, left)
        for ref in kept:
            self.assertIn(ref, left)
        self.assertIn('5 unreadable', done.stdout)

    def test_a_payload_that_is_a_directory_does_not_shift_the_rest(self):
        # cat-file --batch prints a tree's body too. Skipping it only for blobs
        # misread every later object, and could delete a young ref.
        odd = self.publish(files={'payload.json': {'finished': '1\n' * 50,
                                                   'x': 'blob missing\n'},
                                  'signer': 'key\n', 'verdict.sig': 'sig\n'})
        young = self.publish(finished=1 * DAY)
        old = self.publish(finished=90 * DAY)
        done = self.prune()
        self.assertEqual(done.returncode, 0, done.stderr)
        left = self.remote_refs()
        self.assertIn(odd, left)
        self.assertIn(young, left)
        self.assertNotIn(old, left)
        self.assertIn('3 refs, 1 older than 30 days, 1 deleted, 1 kept, 0 changed, '
                      '1 unreadable', done.stdout)

    def test_a_listing_larger_than_one_environment_string(self):
        # Linux caps one environment string at 128 KiB, about 1,200 listing
        # lines. 2,000 refs must still reach the payload reader.
        old = self.remote_oid(self.publish(finished=90 * DAY))
        young = self.remote_oid(self.publish(finished=1 * DAY))
        count = 2000
        made = ''.join('create refs/pandora/verdicts/%040x/suite %s\n'
                       % (0x100000 + i, old if i % 2 else young) for i in range(count))
        git(self.origin, 'update-ref', '--stdin', stdin=made.encode())
        done = self.prune()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('%d refs, %d older than 30 days, %d deleted, %d kept, 0 changed, '
                      '0 unreadable' % (count + 2, count // 2 + 1, count // 2 + 1,
                                        count // 2 + 1), done.stdout)
        left = self.remote_refs()
        self.assertEqual(len([r for r in left if r.startswith('refs/pandora/verdicts/')]),
                         count // 2 + 1)

    def test_a_stale_lease_keeps_that_ref_and_deletes_the_rest_of_the_batch(self):
        old = [self.publish(finished=d * DAY) for d in (40, 50, 60)]
        newer = self.remote_oid(self.publish(finished=70 * DAY))
        # A git that moves old[1] on origin just before the first push, as a
        # client publishing between the listing and the delete would.
        shim = self.root / 'shim'
        shim.mkdir()
        mark = self.root / 'moved'
        (shim / 'git').write_text(
            '#!/bin/sh\n'
            'if [ "$1" = push ] && [ ! -e %s ]; then\n'
            '    : >%s\n'
            '    %s -C %s update-ref %s %s\n'
            'fi\n'
            'exec %s "$@"\n' % (mark, mark, shutil.which('git'), self.origin, old[1], newer,
                                 shutil.which('git')))
        (shim / 'git').chmod(0o755)
        done = self.prune('--batch', '10', path=shim)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(mark.exists())
        left = self.remote_refs()
        self.assertIn(old[1], left)
        self.assertNotIn(old[0], left)
        self.assertNotIn(old[2], left)
        self.assertIn('kept %s: it changed after the listing' % old[1], done.stdout)
        self.assertIn('4 refs, 4 older than 30 days, 3 deleted, 0 kept, 1 changed',
                      done.stdout)

    def test_a_rejected_delete_fails_and_the_rest_of_the_batch_is_counted(self):
        old = [self.publish(finished=d * DAY) for d in (40, 50, 60)]
        hook = self.origin / 'hooks' / 'update'
        hook.write_text('#!/bin/sh\n[ "$1" != %s ]\n' % old[1])
        hook.chmod(0o755)
        done = self.prune()
        self.assertEqual(done.returncode, 1, done.stdout)
        left = self.remote_refs()
        self.assertIn(old[1], left)
        self.assertNotIn(old[0], left)
        self.assertNotIn(old[2], left)
        self.assertIn('delete %s failed' % old[1], done.stderr)
        self.assertIn('3 refs, 3 older than 30 days, 2 deleted, 0 kept, 0 changed',
                      done.stdout)

    def test_refs_outside_the_verdict_namespace_are_untouched(self):
        other = self.publish(finished=90 * DAY, ref='refs/pandora/other/x')
        odd = self.publish(finished=90 * DAY, ref='refs/pandora/verdicts/not-a-tree/suite')
        done = self.prune()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertLessEqual({other, odd, 'refs/heads/main'}, self.remote_refs())

    def test_a_dry_run_deletes_nothing(self):
        old = self.publish(finished=90 * DAY)
        done = self.prune('--dry-run')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(old, self.remote_refs())
        self.assertIn('would delete %s' % old, done.stdout)

    def test_no_verdict_refs_is_a_quiet_success(self):
        done = self.prune()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('no verdict refs', done.stdout)

    def test_the_scratch_namespace_is_removed(self):
        self.publish(finished=1 * DAY)
        done = self.prune()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(git(self.root / 'ci', 'for-each-ref', 'refs/pandora-prune/'), '')

    def test_bad_arguments_exit_2(self):
        old = self.publish(finished=90 * DAY)
        for args in (['--max-age-days', 'ten'], ['--max-age-days', '0'],
                     ['--max-age-days', '00'], ['--batch', '0'], ['--remote', '-x'],
                     ['--bogus']):
            done = self.prune(*args)
            self.assertEqual(done.returncode, 2, args)
        self.assertIn(old, self.remote_refs())

    def test_an_unreachable_remote_exits_1(self):
        self.prune('--dry-run')
        git(self.root / 'ci', 'remote', 'set-url', 'origin', str(self.root / 'gone.git'))
        self.assertEqual(self.prune().returncode, 1)


if __name__ == '__main__':
    unittest.main()
