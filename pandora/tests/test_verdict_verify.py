"""scripts/verdict-verify.sh: what CI accepts as a signed verdict for a tree.

Each case builds a bare `origin`, publishes a verdict ref there the way the
client does (a parentless commit made with plumbing), clones it as CI would,
and runs the script. The signing key is a throwaway made here.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'verdict-verify.sh'
ARGV = ['python3', '-m', 'unittest', 'discover', '-s', 'pandora']
SIGNERS = '.github/pandora/allowed_signers'
HEADER = '# pandora-verdict namespaces="pandora-verdict" ssh-ed25519 AAAA...\n'


def git(repo, *args, stdin=None):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, input=stdin,
                          capture_output=True).stdout


def keypair(path):
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'pandora-verdict',
                    '-f', str(path)], check=True, capture_output=True)
    return path.with_suffix('.pub').read_text().strip()


def signers_line(public):
    return 'pandora-verdict namespaces="pandora-verdict" %s\n' % public


@unittest.skipUnless(shutil.which('ssh-keygen') and shutil.which('bash'),
                     'ssh-keygen and bash required')
class VerdictVerifyTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='pandora-verdict-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.key = self.root / 'keys' / 'verdict'
        self.key.parent.mkdir()
        self.public = keypair(self.key)
        self.origin = self.root / 'origin.git'
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(self.origin)],
                       check=True)
        self.dev = self.root / 'dev'
        subprocess.run(['git', 'clone', '-q', str(self.origin), str(self.dev)],
                       check=True, capture_output=True)
        git(self.dev, 'config', 'user.email', 'test@example.com')
        git(self.dev, 'config', 'user.name', 'Test')

    # --- building ---------------------------------------------------------

    def commit(self, files, message, branch):
        git(self.dev, 'checkout', '-q', '-B', branch)
        for name, text in files.items():
            path = self.dev / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(self.dev, 'add', '-A')
        git(self.dev, 'commit', '-qm', message)
        git(self.dev, 'push', '-q', 'origin', branch)
        return git(self.dev, 'rev-parse', 'HEAD^{tree}').decode().strip()

    def base_and_head(self, base_signers=None, head_files=None):
        """main with a signers file, then a change branch; returns the head tree."""
        text = HEADER + (signers_line(self.public) if base_signers is None else base_signers)
        self.commit({SIGNERS: text, 'README': 'base\n'}, 'base', 'main')
        return self.commit(dict({'feature.py': 'x = 1\n'}, **(head_files or {})),
                           'change', 'change')

    def publish(self, tree, job='suite', key=None, fields=None):
        """Push a verdict to refs/pandora/verdicts/<tree>/<job>; `fields` override the payload."""
        payload = {'argv': ARGV, 'engine': 'e1', 'finished': 1791209006.46,
                   'golden': '0123456789abcdef', 'input_id': 'a' * 64, 'job': job,
                   'kind': 'pandora-verdict', 'outcome': 'passed', 'repo': 'pandora',
                   'run_id': 'r42', 'tree': tree, 'v': 1}
        payload.update(fields or {})
        data = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        key = key or self.key
        signature = subprocess.run(['ssh-keygen', '-Y', 'sign', '-f', str(key),
                                    '-n', 'pandora-verdict'], input=data, check=True,
                                   capture_output=True).stdout
        signer = key.with_suffix('.pub').read_bytes()
        blobs = [(name, git(self.dev, 'hash-object', '-w', '--stdin', stdin=body)
                  .decode().strip())
                 for name, body in (('payload.json', data), ('signer', signer),
                                    ('verdict.sig', signature))]
        listing = ''.join('100644 blob %s\t%s\n' % (oid, name) for name, oid in blobs)
        made = git(self.dev, 'mktree', stdin=listing.encode()).decode().strip()
        env = dict(os.environ, GIT_AUTHOR_NAME='pandora', GIT_AUTHOR_EMAIL='pandora@localhost',
                   GIT_AUTHOR_DATE='@1 +0000', GIT_COMMITTER_NAME='pandora',
                   GIT_COMMITTER_EMAIL='pandora@localhost', GIT_COMMITTER_DATE='@1 +0000')
        commit = subprocess.run(['git', '-C', str(self.dev), 'commit-tree', made, '-m',
                                 'pandora verdict %s %s' % (tree, job)], env=env,
                                check=True, capture_output=True).stdout.decode().strip()
        git(self.dev, 'push', '-q', 'origin',
            '%s:refs/pandora/verdicts/%s/%s' % (commit, tree, job))

    # --- verifying --------------------------------------------------------

    def verify(self, *, job='suite', argv=ARGV, base='origin/main', branch='change',
               env=None):
        ci = self.root / 'ci'
        if not ci.exists():
            subprocess.run(['git', 'clone', '-q', str(self.origin), str(ci)],
                           check=True, capture_output=True)
        git(ci, 'checkout', '-q', branch)
        output = self.root / 'github_output'
        command = ['bash', str(SCRIPT), '--job', job, '--argv', json.dumps(argv)]
        if base is not None:
            command += ['--base', base]
        environment = dict(os.environ, GITHUB_OUTPUT=str(output))
        environment.pop('GITHUB_BASE_REF', None)
        environment.update(env or {})
        done = subprocess.run(command, cwd=ci, env=environment, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('::notice::pandora verdict:', done.stdout)
        fields = dict(line.split('=', 1) for line in output.read_text().splitlines())
        output.unlink()
        return fields

    def test_a_signed_verdict_for_this_tree_verifies(self):
        tree = self.base_and_head()
        self.publish(tree)
        fields = self.verify()
        self.assertEqual(fields, {'verified': 'true', 'reason': 'match', 'run_id': 'r42',
                                  'golden': '0123456789abcdef'})

    def test_a_payload_for_another_tree_is_refused(self):
        tree = self.base_and_head()
        self.publish(tree, fields={'tree': 'b' * 40})
        self.assertEqual(self.verify()['reason'], 'tree_mismatch')

    def test_a_head_with_another_tree_finds_no_verdict(self):
        tree = self.base_and_head()
        self.publish(tree)
        self.commit({'feature.py': 'x = 2\n'}, 'edit', 'change')
        fields = self.verify()
        self.assertEqual((fields['verified'], fields['reason']), ('false', 'no_verdict'))

    def test_a_payload_for_another_job_is_refused(self):
        tree = self.base_and_head()
        self.publish(tree, fields={'job': 'lint'})
        self.assertEqual(self.verify()['reason'], 'job_mismatch')

    def test_a_verdict_for_another_job_is_not_found(self):
        tree = self.base_and_head()
        self.publish(tree, job='lint')
        self.assertEqual(self.verify()['reason'], 'no_verdict')

    def test_another_argv_is_refused(self):
        tree = self.base_and_head()
        self.publish(tree)
        fields = self.verify(argv=ARGV + ['-v'])
        self.assertEqual((fields['verified'], fields['reason']), ('false', 'argv_mismatch'))

    def test_a_failed_outcome_is_refused(self):
        tree = self.base_and_head()
        self.publish(tree, fields={'outcome': 'failed'})
        self.assertEqual(self.verify()['reason'], 'not_passed')

    def test_a_key_not_in_the_signers_file_is_a_bad_signature(self):
        tree = self.base_and_head()
        stranger = self.root / 'keys' / 'stranger'
        keypair(stranger)
        self.publish(tree, key=stranger)
        fields = self.verify()
        self.assertEqual((fields['verified'], fields['reason']), ('false', 'bad_signature'))
        self.assertEqual(fields['run_id'], '')

    def test_a_missing_ref_is_a_miss(self):
        self.base_and_head()
        self.assertEqual(self.verify(), {'verified': 'false', 'reason': 'no_verdict',
                                         'run_id': '', 'golden': ''})

    def test_an_empty_signers_file_verifies_nothing(self):
        tree = self.base_and_head(base_signers='')
        self.publish(tree)
        fields = self.verify()
        self.assertEqual((fields['verified'], fields['reason']), ('false', 'no_signers'))

    def test_signers_are_read_from_the_base_not_the_head(self):
        # The change adds its own key to the signers file and signs with it.
        intruder = self.root / 'keys' / 'intruder'
        public = keypair(intruder)
        tree = self.base_and_head(head_files={SIGNERS: HEADER + signers_line(public)})
        self.publish(tree, key=intruder)
        self.assertEqual(self.verify()['reason'], 'bad_signature')
        # The same verdict checked against the head's own file would pass:
        # the base is the only thing refusing it.
        self.assertEqual(self.verify(base='HEAD')['reason'], 'match')

    def test_a_pull_request_reads_signers_from_its_base_branch(self):
        tree = self.base_and_head(base_signers='')
        self.publish(tree)
        # The clone's origin/main has no key; only the fetch of main brings one.
        self.assertEqual(self.verify()['reason'], 'no_signers')
        self.commit({SIGNERS: HEADER + signers_line(self.public)}, 'add key', 'main')
        fields = self.verify(base=None, env={'GITHUB_BASE_REF': 'main'})
        self.assertEqual((fields['verified'], fields['reason']), ('true', 'match'))

    def test_a_job_that_cannot_name_a_ref_is_refused(self):
        self.base_and_head()
        self.assertEqual(self.verify(job='../x')['reason'], 'bad_job')


if __name__ == '__main__':
    unittest.main()
