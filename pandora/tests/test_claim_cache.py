"""Each worktree routes by a claim cache derived from its own pandora.toml.

Through the real POSIX shim: which file it reads, when it trusts it, and that
the non-enrolled and fresh-cache paths start no Python (`PANDORA_PYTHON` names
nothing, so a Python start would fail the command).
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from pandora.client import enrollment
from pandora.tests.test_fallback import DaemonCase

HERE = Path(__file__).resolve().parents[2]
SHELLS = ['sh'] + [shell for shell in ('/bin/dash', '/usr/bin/dash') if os.path.exists(shell)][:1]
NO_PYTHON = '/nonexistent/python3'


def age(path, seconds=60):
    """Date a file `seconds` ago; returns the mtime it now has."""
    stamp = time.time_ns() - seconds * 10**9
    os.utime(path, ns=(stamp, stamp))
    return Path(path).stat().st_mtime_ns


class Paths(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(home.name).resolve()

    def test_the_cache_lives_in_each_worktrees_own_git_dir(self):
        main = self.root / 'main'
        (main / '.git' / 'worktrees' / 'b').mkdir(parents=True)
        branch = self.root / 'b'
        branch.mkdir()
        (branch / '.git').write_text('gitdir: %s\n' % (main / '.git' / 'worktrees' / 'b'))
        (main / '.git' / 'worktrees' / 'b' / 'gitdir').write_text(str(branch / '.git') + '\n')
        self.assertEqual(enrollment.cache_path(main), main / '.git' / 'pandora-claims')
        self.assertEqual(enrollment.cache_path(branch),
                         main / '.git' / 'worktrees' / 'b' / 'pandora-claims')
        self.assertEqual(enrollment.caches_of(main / '.git'),
                         [(str(main), main / '.git' / 'pandora-claims'),
                          (str(branch), main / '.git' / 'worktrees' / 'b' / 'pandora-claims')])

    def test_a_cache_is_dated_as_its_newest_source(self):
        one, two, cache = self.root / 'one', self.root / 'two', self.root / 'cache'
        one.write_text('1')
        two.write_text('2')
        age(one, 120)
        newest = age(two, 60)
        self.assertTrue(enrollment.write_cache(cache, 'x\n', [one, two, None]))
        self.assertEqual(cache.stat().st_mtime_ns, newest)
        self.assertFalse(enrollment.write_cache(cache, 'x\n', [one, two]))   # nothing to do

    def test_a_source_edited_just_now_leaves_the_cache_dated_before_it(self):
        source, cache = self.root / 'source', self.root / 'cache'
        source.write_text('1')
        enrollment.write_cache(cache, 'x\n', [source])
        self.assertLessEqual(cache.stat().st_mtime_ns,
                             source.stat().st_mtime_ns - enrollment.SETTLE_NS)
        # Once it settles, the same text is re-dated exactly: a rewrite, not a no-op.
        stamp = age(source, 10)
        self.assertTrue(enrollment.write_cache(cache, 'x\n', [source]))
        self.assertEqual(cache.stat().st_mtime_ns, stamp)


class FreshnessInPython(unittest.TestCase):
    """`cache_state` is the shim's rule, for `pandora doctor`."""

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(home.name).resolve()
        self.repo = self.root / 'repo'
        (self.repo / '.git').mkdir(parents=True)
        self.client = self.root / 'config.toml'
        self.external = self.root / 'external.toml'

    def derive(self, config=''):
        repo = {'name': 'demo', 'root': str(self.repo), 'config': config}
        text, sources = enrollment.derive(self.repo, repo, socket_path='/s', client=self.client)
        cache = self.repo / '.git' / 'pandora-claims'
        enrollment.write_cache(cache, text, sources)
        age(cache, 30)
        return cache, enrollment.parse(text)

    def state(self, cache):
        return enrollment.cache_state(self.repo, cache)[0]

    def test_a_missing_external_config_is_named_and_its_arrival_is_seen(self):
        cache, parsed = self.derive(str(self.external))
        self.assertEqual((parsed['derived'], parsed['config']), ('none', str(self.external)))
        self.assertEqual(parsed['noclient'], str(self.client))
        self.assertIn('# claims nothing:', cache.read_text())
        self.assertEqual(self.state(cache), 'fresh')
        self.external.write_text('')
        age(self.external, 120)
        self.assertEqual(self.state(cache), 'stale')

    def test_a_deleted_own_file_is_stale(self):
        (self.repo / 'pandora.toml').write_text('broken')
        age(self.repo / 'pandora.toml', 60)
        cache, parsed = self.derive()
        self.assertEqual(parsed['derived'], 'own')
        self.assertEqual(self.state(cache), 'fresh')
        (self.repo / 'pandora.toml').unlink()
        self.assertEqual(self.state(cache), 'stale')

    def test_the_client_config_appearing_is_stale(self):
        cache, _parsed = self.derive()
        self.assertEqual(self.state(cache), 'fresh')
        self.client.write_text('')
        age(self.client, 120)
        self.assertEqual(self.state(cache), 'stale')


class ThroughTheShim(unittest.TestCase):
    """The shell alone: the package it hands off to prints how it was called."""

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(home.name).resolve()
        self.repo, fake, self.package = self.root / 'repo', self.root / 'fake', self.root / 'pkg'
        for directory in (self.repo, fake, self.package / 'pandora' / 'client'):
            directory.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (fake / 'pnpm').write_text('#!/bin/sh\necho "real $*"\n')
        (fake / 'pnpm').chmod(0o755)
        for name in ('pandora/__init__.py', 'pandora/client/__init__.py'):
            (self.package / name).write_text('')
        (self.package / 'pandora' / 'client' / 'shim.py').write_text(
            'import sys\nhead = sys.argv[1:sys.argv.index("--")]\n'
            'print(" ".join(["client"] + [a for a in head if a == "--refresh"] + ["--"]\n'
            '               + sys.argv[sys.argv.index("--") + 1:]))\n')
        self.toml = self.repo / 'pandora.toml'
        self.toml.write_text('# the truth\n')
        self.client = self.root / 'config.toml'
        self.client.write_text('')
        age(self.client, 120)
        age(self.toml, 60)
        self.env = dict(os.environ, PATH='%s:%s' % (HERE / 'bin', fake) + ':/usr/bin:/bin')
        for name in ('PANDORA_OFF', 'PANDORA_ROUTE_DEPTH', 'PANDORA_HOME', 'PANDORA_WHERE',
                     'PANDORA_PYTHON'):
            self.env.pop(name, None)

    def write(self, path, claims, **extra):
        text = enrollment.render(socket_path=str(self.root / 'client.sock'), repo='demo',
                                 claims=claims, heavy=enrollment.heavy_forms(claims),
                                 home=str(self.package), **extra)
        Path(path).write_text(text)
        return Path(path)

    def cache(self, claims=(('journey',),), git=None, **extra):
        extra.setdefault('derived', 'own')
        if 'noclient' not in extra:
            extra.setdefault('client', str(self.client))
        path = self.write((git or self.repo / '.git') / 'pandora-claims',
                          [list(claim) for claim in claims], **extra)
        age(path, 30)
        return path

    def pnpm(self, *argv, cwd=None, **extra):
        outputs = set()
        for shell in SHELLS:
            proc = subprocess.run([shell, str(HERE / 'bin' / 'pnpm'), *argv],
                                  cwd=cwd or self.repo, env=dict(self.env, **extra),
                                  capture_output=True, text=True, timeout=30)
            outputs.add((proc.returncode, proc.stdout.strip(), proc.stderr))
        self.assertEqual(len(outputs), 1, outputs)
        return outputs.pop()[1]

    def test_a_fresh_cache_is_used_as_it_stands_and_starts_no_python_for_the_unclaimed(self):
        self.cache()
        self.assertEqual(self.pnpm('journey', 'x'), 'client -- journey x')
        self.assertEqual(self.pnpm('why', PANDORA_PYTHON=NO_PYTHON), 'real why')

    def test_a_pandora_toml_newer_than_the_cache_takes_the_slow_path(self):
        self.cache()
        age(self.toml, 0)
        self.assertEqual(self.pnpm('why'), 'client --refresh -- why')
        self.assertEqual(self.pnpm('journey'), 'client --refresh -- journey')

    def test_a_client_config_newer_than_the_cache_takes_the_slow_path(self):
        self.cache()
        age(self.client, 0)
        self.assertEqual(self.pnpm('why'), 'client --refresh -- why')

    def test_an_external_config_is_checked_by_its_own_date(self):
        self.toml.unlink()
        external = self.root / 'external.toml'
        external.write_text('')
        age(external, 60)
        self.cache(config=str(external), derived='external')
        self.assertEqual(self.pnpm('why', PANDORA_PYTHON=NO_PYTHON), 'real why')
        age(external, 0)
        self.assertEqual(self.pnpm('why'), 'client --refresh -- why')

    def test_a_pandora_toml_appearing_overrides_the_external_config(self):
        external = self.root / 'external.toml'
        external.write_text('')
        age(external, 60)
        self.cache(config=str(external), derived='external')
        age(self.toml, 120)                     # older than the cache, and still it wins
        self.assertEqual(self.pnpm('why'), 'client --refresh -- why')

    def test_a_deleted_pandora_toml_makes_its_cache_stale(self):
        self.cache()
        self.toml.unlink()
        self.assertEqual(self.pnpm('why'), 'client --refresh -- why')

    def test_a_deleted_external_config_makes_its_cache_stale(self):
        self.toml.unlink()
        external = self.root / 'external.toml'
        external.write_text('')
        age(external, 60)
        self.cache(config=str(external), derived='external')
        external.unlink()
        self.assertEqual(self.pnpm('why'), 'client --refresh -- why')

    def test_a_cache_derived_from_nothing_goes_stale_when_either_file_appears(self):
        self.toml.unlink()
        external = self.root / 'external.toml'
        self.cache(claims=(), config=str(external), derived='none')
        self.assertEqual(self.pnpm('journey', PANDORA_PYTHON=NO_PYTHON), 'real journey')
        external.write_text('')
        age(external, 120)                      # older than the cache: appearing is enough
        self.assertEqual(self.pnpm('why'), 'client --refresh -- why')
        external.unlink()
        self.toml.write_text('')
        age(self.toml, 120)
        self.assertEqual(self.pnpm('why'), 'client --refresh -- why')

    def test_the_client_config_appearing_or_going_makes_the_cache_stale(self):
        self.client.unlink()
        self.cache(noclient=str(self.client))
        self.assertEqual(self.pnpm('why', PANDORA_PYTHON=NO_PYTHON), 'real why')
        self.client.write_text('')
        age(self.client, 120)
        self.assertEqual(self.pnpm('why'), 'client --refresh -- why')
        self.cache()
        self.client.unlink()
        self.assertEqual(self.pnpm('why'), 'client --refresh -- why')

    def test_a_cache_that_does_not_say_what_it_came_from_is_stale(self):
        self.cache(derived='')
        self.assertEqual(self.pnpm('why'), 'client --refresh -- why')

    def test_registration_without_a_cache_takes_the_slow_path(self):
        self.write(self.repo / '.git' / 'pandora-repo', [])
        self.assertEqual(self.pnpm('why'), 'client --refresh -- why')

    def test_the_v02_marker_is_used_when_there_is_no_cache(self):
        self.write(self.repo / '.git' / 'pandora-enrolled', [['journey']])
        self.assertEqual(self.pnpm('journey'), 'client -- journey')
        self.assertEqual(self.pnpm('why', PANDORA_PYTHON=NO_PYTHON), 'real why')
        # A cache, once the daemon has written one, wins over the marker.
        self.cache(claims=(('check',),))
        self.assertEqual(self.pnpm('journey', PANDORA_PYTHON=NO_PYTHON), 'real journey')
        self.assertEqual(self.pnpm('check'), 'client -- check')

    def test_registration_wins_over_the_v02_marker(self):
        self.write(self.repo / '.git' / 'pandora-enrolled', [['journey']])
        self.write(self.repo / '.git' / 'pandora-repo', [])
        self.assertEqual(self.pnpm('journey'), 'client --refresh -- journey')

    def test_nothing_enrolled_execs_with_no_python(self):
        self.assertEqual(self.pnpm('journey', PANDORA_PYTHON=NO_PYTHON), 'real journey')

    def test_a_linked_worktree_reads_its_own_cache_not_the_main_ones(self):
        self.cache(claims=(('journey',),))
        subprocess.run(['git', '-C', str(self.repo), '-c', 'commit.gpgsign=false', 'commit', '-q',
                        '--allow-empty', '-m', 'x'],
                       check=True, env=dict(self.env, GIT_AUTHOR_NAME='t', GIT_COMMITTER_NAME='t',
                                            GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_EMAIL='t@t'))
        branch = self.root / 'branch'
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '-q', str(branch)],
                       check=True)
        (branch / 'pandora.toml').write_text('')
        age(branch / 'pandora.toml', 60)
        own = self.repo / '.git' / 'worktrees' / 'branch'
        self.cache(claims=(('check',),), git=own)
        self.assertEqual(self.pnpm('check', cwd=branch), 'client -- check')
        self.assertEqual(self.pnpm('journey', cwd=branch, PANDORA_PYTHON=NO_PYTHON),
                         'real journey')
        self.assertEqual(self.pnpm('journey'), 'client -- journey')

    def test_pandora_off_takes_a_stale_cache_at_its_word(self):
        self.cache()
        age(self.toml, 0)
        self.assertEqual(self.pnpm('why', PANDORA_OFF='1', PANDORA_PYTHON=NO_PYTHON),
                         'real why')


class SlowPathAgainstARealDaemon(DaemonCase):
    """Stale or missing: one Python start, the daemon rewrites the cache, then none."""

    def setUp(self):
        super().setUp()
        (self.repo / '.git').mkdir(exist_ok=True)
        (self.repo / '.git' / 'pandora-repo').write_text(enrollment.registration_text(
            socket_path=str(self.daemon.socket_path), repo='demo', home=str(HERE)))
        age(self.root / 'config.toml', 120)
        age(self.repo / 'pandora.toml', 60)
        self.cache = self.repo / '.git' / 'pandora-claims'
        fake = self.root / 'fake'
        fake.mkdir()
        (fake / 'pnpm').write_text('#!/bin/sh\necho "real $*"\n')
        (fake / 'pnpm').chmod(0o755)
        self.env = dict(os.environ, PATH='%s:%s' % (HERE / 'bin', fake) + ':/usr/bin:/bin')
        for name in ('PANDORA_OFF', 'PANDORA_ROUTE_DEPTH', 'PANDORA_HOME', 'PANDORA_WHERE',
                     'PANDORA_PYTHON'):
            self.env.pop(name, None)

    def pnpm(self, *argv, **extra):
        return subprocess.run(['sh', str(HERE / 'bin' / 'pnpm'), *argv], cwd=self.repo,
                              env=dict(self.env, **extra), capture_output=True, text=True,
                              timeout=60)

    def rows(self):
        path = self.state / 'passthrough.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] \
            if path.exists() else []

    def test_a_missing_cache_is_written_and_the_next_command_forks_nothing(self):
        proc = self.pnpm('why', 'react')
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, 'real why react\n', ''))
        self.assertEqual(enrollment.cache_state(self.repo, self.cache)[0], 'fresh')
        proc = self.pnpm('why', PANDORA_PYTHON=NO_PYTHON)
        self.assertEqual((proc.returncode, proc.stdout), (0, 'real why\n'), proc.stderr)
        self.assertEqual(self.rows(), [])

    def test_a_stale_cache_is_rewritten_from_the_changed_file(self):
        self.cache.write_text(enrollment.render(
            socket_path=str(self.daemon.socket_path), repo='demo', claims=[['old']],
            home=str(HERE)))
        age(self.cache, 90)                     # older than pandora.toml: stale
        proc = self.pnpm('old')
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, 'real old\n', ''))
        cache = enrollment.parse(self.cache.read_text())
        self.assertIn(['unit'], cache['claim'])
        self.assertNotIn(['old'], cache['claim'])
        self.assertEqual(enrollment.cache_state(self.repo, self.cache)[0], 'fresh')

    def test_a_heavy_unclaimed_command_on_the_slow_path_is_still_counted(self):
        proc = self.pnpm('build')
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, 'real build\n', ''))
        [row] = self.rows()
        self.assertEqual((row['reason'], row['argv']), ('unclaimed', ['build']))

    def test_a_claimed_command_on_the_slow_path_is_routed(self):
        proc = self.pnpm('unit')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('on the worker', proc.stderr)
        self.assertTrue(self.cache.is_file())

    def test_with_no_daemon_the_stale_cache_decides_and_nothing_is_refused(self):
        self.cache.write_text(enrollment.render(
            socket_path=str(self.root / 'nobody.sock'), repo='demo', claims=[['unit']],
            home=str(HERE)))
        age(self.cache, 90)
        proc = self.pnpm('why')
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, 'real why\n', ''))
        proc = self.pnpm('unit')
        self.assertEqual((proc.returncode, proc.stdout), (0, 'real unit\n'))
        self.assertIn('as if Pandora were not installed', proc.stderr)


class EnrollOnce(unittest.TestCase):
    """`pandora enroll` registers a repository once; a later pandora.toml needs nothing."""

    TOML = ('version = 1\n[repo]\nname = "demo"\nentrypoints = ["pnpm"]\n'
            '[worker]\nbase_image = "images:ubuntu/26.04"\n'
            '[[jobs]]\nid = "j"\nargs = "none"\nforms = [{ prefix = ["journey"] }]\n'
            'run = { argv = ["true"] }\n')

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(home.name).resolve()
        self.repo = self.root / 'repo'
        (self.repo / '.git' / 'worktrees' / 'b').mkdir(parents=True)
        (self.repo / 'pandora.toml').write_text(self.TOML)
        self.config = self.root / 'cfg' / 'config.toml'
        self.config.parent.mkdir()
        self.config.write_text('# mine\n[client]\nstate = "%s"\n' % (self.root / 'state'))

    def pandora(self, *argv):
        from pandora import cli
        from pandora.tests.test_cli import capture
        return capture(cli.main, ['--config', str(self.config), *argv])

    def test_enroll_registers_writes_this_worktrees_cache_and_adds_repos(self):
        (self.repo / '.git' / 'pandora-enrolled').write_text('sock /old\nclaim old\n')
        code, _out, err = self.pandora('enroll', str(self.repo))
        self.assertEqual(code, 0, err)
        self.assertTrue((self.repo / '.git' / 'pandora-repo').is_file())
        self.assertFalse((self.repo / '.git' / 'pandora-enrolled').exists())
        cache = enrollment.parse((self.repo / '.git' / 'pandora-claims').read_text())
        self.assertEqual(cache['claim'], [['journey']])
        self.assertEqual(cache['client'], str(self.config))
        text = self.config.read_text()
        self.assertTrue(text.startswith('# mine\n[client]\n'))
        self.assertIn('[[repos]]\nname = "demo"\nroot = "%s"\n' % self.repo, text)
        self.assertIn('added [[repos]] demo', err)
        self.assertIn('removed the v0.2 marker', err)

    def test_enrolling_again_adds_nothing_and_a_changed_toml_needs_no_enroll(self):
        self.pandora('enroll', str(self.repo))
        before = self.config.read_text()
        code, _out, err = self.pandora('enroll', str(self.repo))
        self.assertEqual((code, self.config.read_text()), (0, before), err)
        self.assertNotIn('added', err)
        # The file changes; the cache the shim reads is now stale, which sends
        # the next command to the daemon. No enroll is involved.
        cache = self.repo / '.git' / 'pandora-claims'
        age(cache, 30)
        (self.repo / 'pandora.toml').write_text(self.TOML.replace('journey', 'check'))
        self.assertEqual(enrollment.cache_state(self.repo, cache)[0], 'stale')

    def test_an_entry_under_another_name_is_left_alone_and_the_block_printed(self):
        with self.config.open('a') as handle:
            handle.write('[[repos]]\nname = "mine"\nroot = "%s"\n' % self.repo)
        before = self.config.read_text()
        code, _out, err = self.pandora('enroll', str(self.repo))
        self.assertEqual((code, self.config.read_text()), (0, before))
        self.assertIn('left as it is', err)
        self.assertIn('name = "demo"', err)

    def test_unenroll_removes_registration_marker_and_every_cache(self):
        self.pandora('enroll', str(self.repo))
        (self.repo / '.git' / 'worktrees' / 'b' / 'pandora-claims').write_text('sock /s\n')
        (self.repo / '.git' / 'pandora-enrolled').write_text('sock /s\n')
        code, _out, err = self.pandora('unenroll', str(self.repo))
        self.assertEqual(code, 0, err)
        for name in ('pandora-repo', 'pandora-enrolled', 'pandora-claims',
                     'worktrees/b/pandora-claims'):
            self.assertFalse((self.repo / '.git' / name).exists(), name)
        self.assertIn('[[repos]] demo is still in', err)
