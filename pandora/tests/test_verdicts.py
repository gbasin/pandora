"""Signed run verdicts, client side: the opt-in, the ref, the background push.

Git is real here: a scratch repository, a bare `origin` beside it, and a
linked worktree, so `ls-remote` and `push` run exactly as they do against a
hosted remote, only over a path. The daemon test reuses the fallback suite's
real daemon with a fake worker whose result carries a verdict.
"""
import base64
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pandora import cli
from pandora.client import verdicts
from pandora.config import loader
from pandora.errors import ConfigError, UnknownSchema
from pandora.tests.test_cli import capture
from pandora.tests.test_fallback import DaemonCase, FakeWorker

TREE = 'a' * 40
OTHER_TREE = 'b' * 40
SIGNER = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFakeFakeFakeFakeFakeFakeFakeFake pandora-verdict'
SIGNATURE = '-----BEGIN SSH SIGNATURE-----\nU1NIU0lH\n-----END SSH SIGNATURE-----\n'


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'))


def payload(tree=TREE, job='unit', run_id='r1'):
    return canonical({'argv': ['sh'], 'engine': 'e1', 'finished': 1.5, 'golden': '0' * 16,
                      'input_id': 'f' * 64, 'job': job, 'kind': 'pandora-verdict',
                      'outcome': 'passed', 'repo': 'demo', 'run_id': run_id, 'tree': tree,
                      'v': 1})


def signed(tree=TREE, job='unit', run_id='r1'):
    return {'tree': tree, 'verdict_skipped': None,
            'verdict': {'payload': payload(tree, job, run_id), 'signature': SIGNATURE,
                        'signer': SIGNER}}


def sh(*argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def make_repo(root, *, origin=True):
    """A repository with one commit, and a bare `origin` beside it when asked."""
    repo = root / 'repo'
    repo.mkdir(parents=True, exist_ok=True)
    sh('git', 'init', '-q', '-b', 'main', str(repo))
    (repo / 'file.txt').write_text('hello\n')
    sh('git', '-C', str(repo), 'add', 'file.txt')
    sh('git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
       '-c', 'commit.gpgsign=false', 'commit', '-q', '-m', 'one')
    if origin:
        bare = root / 'origin.git'
        sh('git', 'init', '-q', '--bare', str(bare))
        sh('git', '-C', str(repo), 'remote', 'add', 'origin', str(bare))
    return repo


def fake_remote(root, repo, body):
    """Point the repo's origin at `fake::x`, served by a shell script.

    Git runs `git-remote-fake` from PATH for that URL. The script runs `body`
    with `$dir` set to its scratch directory. Answers the PATH to patch in.
    """
    bin_dir = root / 'fake-bin'
    bin_dir.mkdir(exist_ok=True)
    helper = bin_dir / 'git-remote-fake'
    helper.write_text('#!/bin/sh\ndir=%s\n%s\n' % (bin_dir, body))
    helper.chmod(0o755)
    sh('git', '-C', str(repo), 'remote', 'set-url', 'origin', 'fake::x')
    return '%s%s%s' % (bin_dir, os.pathsep, os.environ.get('PATH', ''))


def gone(pid, wait=10):
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class Loader(unittest.TestCase):
    BASE = ('version = 1\n[repo]\nname = "demo"\nentrypoints = ["pnpm"]\n'
            '[worker]\nbase_image = "images:ubuntu/26.04"\n'
            '[[jobs]]\nid = "unit"\nforms = [{ prefix = ["unit"] }]\n'
            'run = { argv = ["true"] }\n')

    def load(self, extra=''):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'pandora.toml'
            # Before the first `[[jobs]]`, a bare key is top level; after it, a
            # `[verdicts]` header is still a top-level table.
            path.write_text(extra + self.BASE if not extra.startswith('[') else self.BASE + extra)
            return loader.load(path)

    def test_absent_means_no_publication_to_origin(self):
        self.assertEqual(self.load()['verdicts'], {'publish': False, 'remote': 'origin'})

    def test_both_keys_load(self):
        config = self.load('[verdicts]\npublish = true\nremote = "upstream"\n')
        self.assertEqual(config['verdicts'], {'publish': True, 'remote': 'upstream'})
        self.assertEqual(self.load('[verdicts]\npublish = true\n')['verdicts']['remote'],
                         'origin')

    def test_an_unknown_key_is_refused_with_the_allowed_set(self):
        with self.assertRaises(UnknownSchema) as caught:
            self.load('[verdicts]\npublish = true\nref = "x"\n')
        self.assertIn('verdicts has unknown key ref; allowed: publish, remote',
                      str(caught.exception))
        self.assertEqual(caught.exception.key, 'verdicts.ref')

    def test_wrong_types_are_refused(self):
        for extra, needle in (
                ('[verdicts]\npublish = "yes"\n', 'verdicts.publish must be true or false'),
                ('[verdicts]\npublish = 1\n', 'verdicts.publish must be true or false'),
                ('[verdicts]\nremote = 3\n', 'verdicts.remote must be a nonempty string'),
                ('[verdicts]\nremote = ""\n', 'verdicts.remote must be a nonempty string'),
                ('[verdicts]\nremote = "--upload-pack=x"\n',
                 'verdicts.remote is not a valid name'),
                ('verdicts = true\n', 'verdicts must be a table')):
            with self.subTest(extra=extra):
                with self.assertRaises(ConfigError) as caught:
                    self.load(extra)
                self.assertIn(needle, str(caught.exception))


class Commit(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(home.name)

    def build(self, repo, tree=TREE, job='unit'):
        parts = verdicts.parts(signed(tree, job))
        return verdicts.build(repo, *parts)

    def test_same_inputs_give_the_same_commit_in_any_repository(self):
        first = make_repo(self.root / 'a', origin=False)
        second = make_repo(self.root / 'b', origin=False)
        commit = self.build(first)
        self.assertRegex(commit, r'^[0-9a-f]{40}$')
        self.assertEqual(self.build(first), commit)
        self.assertEqual(self.build(second), commit)
        self.assertNotEqual(self.build(first, tree=OTHER_TREE), commit)
        self.assertNotEqual(self.build(first, job='other'), commit)

    def test_a_configured_commit_encoding_does_not_change_the_id(self):
        plain = make_repo(self.root / 'a', origin=False)
        latin = make_repo(self.root / 'b', origin=False)
        sh('git', '-C', str(latin), 'config', 'i18n.commitEncoding', 'ISO-8859-1')
        commit = self.build(latin)
        self.assertEqual(commit, self.build(plain))
        body = sh('git', '-C', str(latin), 'cat-file', '-p', commit)
        self.assertNotIn('\nencoding ', body)

    def test_the_commit_is_parentless_fixed_and_holds_three_files(self):
        repo = make_repo(self.root, origin=False)
        commit = self.build(repo)
        body = sh('git', '-C', str(repo), 'cat-file', '-p', commit)
        self.assertNotIn('\nparent ', body)
        self.assertIn('\nauthor pandora <pandora@localhost> 1 +0000\n', body)
        self.assertIn('\ncommitter pandora <pandora@localhost> 1 +0000\n', body)
        self.assertTrue(body.endswith('\n\npandora verdict %s unit' % TREE), body)
        names = sh('git', '-C', str(repo), 'ls-tree', '--name-only', commit).splitlines()
        self.assertEqual(names, ['payload.json', 'signer', 'verdict.sig'])
        show = lambda name: subprocess.run(  # noqa: E731
            ['git', '-C', str(repo), 'cat-file', 'blob', '%s:%s' % (commit, name)],
            check=True, capture_output=True).stdout.decode()
        self.assertEqual(show('payload.json'), payload())
        self.assertEqual(show('verdict.sig'), SIGNATURE)
        self.assertEqual(show('signer'), SIGNER + '\n')

    def test_nothing_in_the_worktree_or_index_changes(self):
        repo = make_repo(self.root, origin=False)
        (repo / 'dirty.txt').write_text('untracked\n')
        (repo / 'file.txt').write_text('edited\n')
        status = sh('git', '-C', str(repo), 'status', '--porcelain')
        index = (repo / '.git' / 'index').read_bytes()
        head = sh('git', '-C', str(repo), 'rev-parse', 'HEAD')
        self.build(repo)
        self.assertEqual(sh('git', '-C', str(repo), 'status', '--porcelain'), status)
        self.assertEqual((repo / '.git' / 'index').read_bytes(), index)
        self.assertEqual(sh('git', '-C', str(repo), 'rev-parse', 'HEAD'), head)
        self.assertEqual((repo / 'file.txt').read_text(), 'edited\n')

    def test_a_payload_that_disagrees_with_the_result_is_refused(self):
        result = signed()
        result['tree'] = OTHER_TREE
        with self.assertRaises(verdicts.Failed):
            verdicts.parts(result)
        broken = signed()
        broken['verdict']['payload'] = canonical({'tree': 'x', 'job': 'unit'})
        with self.assertRaises(verdicts.Failed):
            verdicts.parts(broken)
        hostile = signed(job='../../heads/main')
        with self.assertRaises(verdicts.Failed):
            verdicts.parts(hostile)


class Publish(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(home.name)
        self.repo = make_repo(self.root)
        self.ref = 'refs/pandora/verdicts/%s/unit' % TREE

    def remote_ref(self):
        return sh('git', '-C', str(self.root / 'origin.git'), 'for-each-ref',
                  '--format=%(objectname)', self.ref)

    def test_pushes_once_then_finds_it_present(self):
        record = verdicts.publish(self.repo, 'origin', signed())
        self.assertEqual(record['state'], 'published', record)
        self.assertEqual(record['ref'], self.ref)
        self.assertEqual(self.remote_ref(), record['commit'])
        self.assertEqual(verdicts.line(record), 'verdict published ' + self.ref)
        again = verdicts.publish(self.repo, 'origin', signed())
        self.assertEqual(again['state'], 'present')
        self.assertEqual(self.remote_ref(), record['commit'])

    def test_present_is_skipped_without_a_push(self):
        sh('git', '-C', str(self.repo), 'push', '-q', 'origin', 'HEAD:' + self.ref)
        before = self.remote_ref()
        record = verdicts.publish(self.repo, 'origin', signed())
        self.assertEqual(record['state'], 'present')
        self.assertEqual(self.remote_ref(), before)           # not overwritten

    def test_a_linked_worktree_publishes_from_the_common_object_store(self):
        linked = self.root / 'linked'
        sh('git', '-C', str(self.repo), 'worktree', 'add', '-q', '--detach', str(linked))
        record = verdicts.publish(linked, 'origin', signed())
        self.assertEqual(record['state'], 'published', record)
        self.assertEqual(self.remote_ref(), record['commit'])

    def test_nothing_to_do_says_nothing(self):
        self.assertIsNone(verdicts.publish(self.repo, 'origin',
                                           {'tree': TREE, 'verdict': None,
                                            'verdict_skipped': 'worker_not_ready'}))
        self.assertIsNone(verdicts.publish(self.repo, 'upstream', signed()))
        bare = make_repo(self.root / 'lonely', origin=False)
        self.assertIsNone(verdicts.publish(bare, 'origin', signed()))

    def test_an_unreachable_remote_is_one_failed_record(self):
        sh('git', '-C', str(self.repo), 'remote', 'set-url', 'origin',
           str(self.root / 'nowhere.git'))
        record = verdicts.publish(self.repo, 'origin', signed())
        self.assertEqual(record['state'], 'failed')
        self.assertTrue(verdicts.line(record).startswith('verdict not published: git ls-remote'),
                        record)

    def test_a_hung_remote_times_out_and_its_whole_group_dies(self):
        path = fake_remote(self.root, self.repo,
                           'sleep 60 &\necho $! > "$dir/child"\nwait')
        with mock.patch.dict(os.environ, PATH=path), \
                mock.patch.object(verdicts, 'TIMEOUT', 2):
            started = time.monotonic()
            record = verdicts.publish(self.repo, 'origin', signed())
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(record['state'], 'failed')
        self.assertRegex(record['reason'],
                         r'^git ls-remote timed out after [0-9]+s \(2s of the 2s deadline used\)$')
        child = int((self.root / 'fake-bin' / 'child').read_text())
        self.assertTrue(gone(child), 'the remote helper\'s child outlived the timeout')
        with self.assertRaises(verdicts.Failed) as caught:
            verdicts.git(self.repo, 'status', deadline=time.monotonic() - 1)
        self.assertEqual(str(caught.exception),
                         'git status not started: the 60s deadline is used up')

    def test_one_deadline_covers_every_subprocess(self):
        seen = []
        real = verdicts.git

        def spy(worktree, *args, **kw):
            seen.append((args[0], kw.get('deadline')))
            return real(worktree, *args, **kw)
        with mock.patch.object(verdicts, 'git', spy):
            record = verdicts.publish(self.repo, 'origin', signed())
        self.assertEqual(record['state'], 'published', record)
        self.assertEqual(seen[0][0], 'remote')
        self.assertIn('ls-remote', [name for name, _ in seen])
        self.assertEqual(len({deadline for _, deadline in seen}), 1, seen)
        self.assertIsNotNone(seen[0][1])

    def test_git_never_prompts_and_has_no_terminal(self):
        path = fake_remote(self.root, self.repo,
                           'env > "$dir/env"\n'
                           'python3 -c "import os; print(os.getsid(0))" > "$dir/sid"\n'
                           'exit 1')

        def seen():
            text = (self.root / 'fake-bin' / 'env').read_text().splitlines()
            return dict(line.split('=', 1) for line in text if '=' in line)
        with mock.patch.dict(os.environ, PATH=path):
            os.environ.pop('GIT_SSH_COMMAND', None)
            os.environ.pop('GIT_SSH', None)
            self.assertEqual(verdicts.publish(self.repo, 'origin', signed())['state'], 'failed')
            env = seen()
            self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
            self.assertEqual(env['SSH_ASKPASS_REQUIRE'], 'never')
            self.assertEqual(env['GIT_SSH_COMMAND'], 'ssh -o BatchMode=yes')
            sid = int((self.root / 'fake-bin' / 'sid').read_text())
            self.assertNotEqual(sid, os.getsid(0))
            # The user's own ssh command stands, from the environment or config.
            os.environ['GIT_SSH_COMMAND'] = 'ssh -i mine'
            verdicts.publish(self.repo, 'origin', signed())
            self.assertEqual(seen()['GIT_SSH_COMMAND'], 'ssh -i mine')
            del os.environ['GIT_SSH_COMMAND']
            sh('git', '-C', str(self.repo), 'config', 'core.sshCommand', 'ssh -i mine')
            verdicts.publish(self.repo, 'origin', signed())
            self.assertNotIn('GIT_SSH_COMMAND', seen())

    def test_a_push_that_loses_a_race_finds_the_other_verdict(self):
        other = make_repo(self.root / 'b', origin=False)
        sh('git', '-C', str(other), 'remote', 'add', 'origin', str(self.root / 'origin.git'))
        real = verdicts.git
        first = {}

        def racing(worktree, *args, **kw):
            if args[0] == 'push' and worktree == other and not first:
                # The other publisher lands between our ls-remote and our push.
                first.update(verdicts.publish(self.repo, 'origin', signed(run_id='r0')))
            return real(worktree, *args, **kw)
        with mock.patch.object(verdicts, 'git', racing):
            record = verdicts.publish(other, 'origin', signed(run_id='r1'))
        self.assertEqual(first['state'], 'published', first)
        self.assertEqual(record['state'], 'present', record)
        self.assertEqual(record['commit'], first['commit'])
        self.assertEqual(self.remote_ref(), first['commit'])
        self.assertEqual(verdicts.line(record),
                         'verdict published %s (already on the remote)' % self.ref)

    def test_a_refused_push_with_no_ref_is_a_failure(self):
        hook = self.root / 'origin.git' / 'hooks' / 'pre-receive'
        hook.write_text('#!/bin/sh\necho refused by policy >&2\nexit 1\n')
        hook.chmod(0o755)
        record = verdicts.publish(self.repo, 'origin', signed())
        self.assertEqual(record['state'], 'failed', record)
        self.assertTrue(record['reason'].startswith('git push exited 1'), record)
        self.assertEqual(self.remote_ref(), '')


def verdict_threads():
    """Wait out every background publish, including one still being started."""
    for thread in threading.enumerate():
        if thread.name.startswith('verdict-'):
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    thread.join(timeout=60)
                    break
                except RuntimeError:                  # not started yet
                    time.sleep(0.01)


class DaemonHook(DaemonCase):
    """`deliver` starts the push after the exit, and the exit never depends on it."""

    def setUp(self):
        super().setUp()
        self.result = {'outcome': 'passed', 'cli_exit': 0, **signed()}
        follow = lambda worker, run_id, **kw: (json.loads(json.dumps(self.result)), 0)  # noqa: E731
        patcher = mock.patch.object(FakeWorker, 'follow', follow)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(verdict_threads)
        make_repo(self.root)
        self.calls = []
        real = verdicts.publish

        def spy(*args):
            self.calls.append(args)
            return real(*args)
        patcher = mock.patch.object(verdicts, 'publish', spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def opt_in(self, text='[verdicts]\npublish = true\n'):
        toml = self.repo / 'pandora.toml'
        toml.write_text(toml.read_text() + text)

    def run_unit(self):
        answer = self.call(['unit'])
        verdict_threads()
        [run_dir] = list((self.state / 'runs').iterdir())
        return answer, run_dir

    def log_lines(self, run_dir):
        lines, kinds = [], []
        for raw in (run_dir / 'log').read_bytes().splitlines():
            frame = json.loads(raw)
            kinds.append(frame['t'])
            if frame['t'] == 'log':
                lines.append(base64.b64decode(frame['b64']).decode())
        return ''.join(lines), kinds

    def test_published_after_the_exit_frame(self):
        self.opt_in()
        answer, run_dir = self.run_unit()
        self.assertEqual(answer.exit, 0)
        self.assertNotIn(b'verdict', answer.err)
        text, kinds = self.log_lines(run_dir)
        self.assertIn('pandora: verdict published refs/pandora/verdicts/%s/unit\n' % TREE, text)
        self.assertEqual(kinds[-2:], ['exit', 'log'])
        record = json.loads((run_dir / verdicts.RECORD).read_text())
        self.assertEqual(record['state'], 'published')
        self.assertEqual(json.loads((run_dir / 'result.json').read_text())['verdict'],
                         self.result['verdict'])
        self.assertEqual(len(self.calls), 1)

    def test_a_failed_push_is_one_line_and_the_exit_stands(self):
        self.opt_in()
        sh('git', '-C', str(self.repo), 'remote', 'set-url', 'origin',
           str(self.root / 'nowhere.git'))
        answer, run_dir = self.run_unit()
        self.assertEqual(answer.exit, 0)
        text, _ = self.log_lines(run_dir)
        self.assertEqual(text.count('pandora: verdict not published: '), 1, text)
        self.assertNotIn('pandora: hint: verdict', text)
        self.assertEqual(json.loads((run_dir / 'result.json').read_text())['cli_exit'], 0)

    def test_a_failing_run_keeps_its_exit_with_or_without_a_verdict(self):
        self.opt_in()
        self.result.update(outcome='command_failed', cli_exit=3, verdict=None,
                           verdict_skipped='not_passed')
        answer, run_dir = self.run_unit()
        self.assertEqual(answer.exit, 3)
        self.assertEqual(self.calls, [])
        self.assertFalse((run_dir / verdicts.RECORD).exists())

    def test_no_opt_in_no_remote_or_no_verdict_does_nothing(self):
        cases = (('', None),
                 ('[verdicts]\npublish = false\n', None),
                 ('[verdicts]\npublish = true\nremote = "upstream"\n', None),
                 ('[verdicts]\npublish = true\n', 'no verdict'))
        original = (self.repo / 'pandora.toml').read_text()
        for text, mode in cases:
            with self.subTest(text=text, mode=mode):
                (self.repo / 'pandora.toml').write_text(original + text)
                if mode == 'no verdict':
                    self.result.update(verdict=None, verdict_skipped='worker_not_ready')
                answer, run_dir = self.run_unit()
                self.assertEqual(answer.exit, 0)
                body, _ = self.log_lines(run_dir)
                self.assertNotIn('verdict', body)
                self.assertFalse((run_dir / verdicts.RECORD).exists())
                import shutil
                shutil.rmtree(run_dir)
        # Only the remote-less opt-in reached `publish`, which said nothing.
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], 'upstream')

    def test_a_publisher_that_raises_never_reaches_the_exit(self):
        self.opt_in()
        with mock.patch.object(verdicts, 'publish', side_effect=RuntimeError('boom')):
            answer, run_dir = self.run_unit()
        self.assertEqual(answer.exit, 0)
        text, _ = self.log_lines(run_dir)
        self.assertIn('pandora: verdict not published: RuntimeError: boom\n', text)

    def test_the_push_does_not_delay_the_exit(self):
        self.opt_in()
        gate = self.root / 'fake-bin' / 'gate'
        path = fake_remote(self.root, self.repo,
                           'while [ ! -e "$dir/gate" ]; do sleep 0.05; done\nexit 1')
        self.addCleanup(lambda: gate.touch())
        with mock.patch.dict(os.environ, PATH=path):
            started = time.monotonic()
            answer = self.call(['unit'])
            self.assertEqual(answer.exit, 0)
            self.assertLess(time.monotonic() - started, 20)
            alive = [thread for thread in threading.enumerate()
                     if thread.name.startswith('verdict-') and thread.is_alive()]
            self.assertTrue(alive, 'the publish finished before the exit returned')
            gate.touch()
            verdict_threads()
        [run_dir] = list((self.state / 'runs').iterdir())
        record = json.loads((run_dir / verdicts.RECORD).read_text())
        self.assertEqual(record['state'], 'failed', record)


class ResultVerb(DaemonCase):
    def write(self, run_id, result, record=None):
        directory = self.state / 'runs' / run_id
        directory.mkdir(parents=True)
        (directory / 'result.json').write_text(json.dumps(result))
        if record is not None:
            (directory / verdicts.RECORD).write_text(json.dumps(record))

    def pandora(self, *argv):
        return capture(cli.main, ['--state', str(self.state),
                                  '--config', str(self.root / 'config.toml'), *argv])

    def test_json_carries_tree_verdict_and_skip(self):
        self.write('v1', {'outcome': 'passed', 'cli_exit': 0, **signed()})
        code, out, _ = self.pandora('result', 'v1', '--json')
        self.assertEqual(code, 0)
        shown = json.loads(out)
        self.assertEqual(shown['tree'], TREE)
        self.assertEqual(shown['verdict']['signer'], SIGNER)
        self.assertIsNone(shown['verdict_skipped'])

    def test_one_human_line_per_case(self):
        ref = 'refs/pandora/verdicts/%s/unit' % TREE
        cases = (
            ('s1', signed(), None, '  verdict: signed, tree aaaaaaaaaaaa'),
            ('s2', signed(), {'state': 'published', 'ref': ref}, '  verdict: published ' + ref),
            ('s3', signed(), {'state': 'present', 'ref': ref}, '  verdict: published ' + ref),
            ('s4', signed(), {'state': 'failed', 'ref': ref, 'reason': 'git push exited 1'},
             '  verdict: signed, tree aaaaaaaaaaaa (not published: git push exited 1)'),
            ('n1', {'tree': None, 'verdict': None, 'verdict_skipped': 'no_synthetic_git'},
             None, '  verdict: none (no_synthetic_git)'))
        for run_id, fields, record, line in cases:
            with self.subTest(run_id=run_id):
                self.write(run_id, {'outcome': 'passed', 'cli_exit': 0, **fields}, record)
                code, out, _ = self.pandora('result', run_id)
                self.assertEqual(code, 0)
                self.assertIn(line, out.splitlines())

    def test_an_older_engine_prints_no_verdict_line(self):
        self.write('o1', {'outcome': 'passed', 'cli_exit': 0})
        _code, out, _ = self.pandora('result', 'o1')
        self.assertNotIn('verdict', out)


if __name__ == '__main__':
    unittest.main()
