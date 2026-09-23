"""Side effect 1 and 2: what the shim decides, and what it refuses to re-enter."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import enrolment
from harness import Sandbox


class ShimDecisions(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.start()
        self.box.enrol()

    def test_unenrolled_directory_execs_the_real_pnpm(self):
        result = self.box.pnpm(['test:unit'], cwd=self.box.root)
        self.assertEqual(result.stdout, b'REAL test:unit\n')

    def test_enrolled_but_unclaimed_argv_execs_the_real_pnpm(self):
        result = self.box.pnpm(['lint:fast'])
        self.assertEqual(result.stdout, b'REAL lint:fast\n')

    def test_enrolled_and_claimed_argv_is_routed(self):
        result = self.box.pnpm(['test:unit'])
        self.assertEqual(result.stdout, b'hello\n')

    def test_two_token_claim_matches(self):
        self.box.enrol(claims=(('validate', 'unit'),))
        self.assertEqual(self.box.pnpm(['validate', 'unit']).stdout, b'hello\n')
        self.assertEqual(self.box.pnpm(['validate', 'check']).stdout, b'REAL validate check\n')

    def test_run_prefix_is_stripped_before_matching(self):
        self.assertEqual(self.box.pnpm(['run', 'test:unit']).stdout, b'hello\n')

    def test_pandora_off_forces_local(self):
        result = self.box.pnpm(['test:unit'], PANDORA_OFF='1')
        self.assertEqual(result.stdout, b'REAL test:unit\n')

    def test_recursion_guard_forces_local(self):
        """eichler re-enters this shim up to three times for one `pnpm check`."""
        result = self.box.pnpm(['test:unit'], PANDORA_ROUTE_DEPTH='1')
        self.assertEqual(result.stdout, b'REAL test:unit\n')

    def test_guard_is_exported_to_the_real_pnpm(self):
        spy = self.box.realbin / 'pnpm'
        spy.write_text('#!/bin/sh\necho "depth=${PANDORA_ROUTE_DEPTH:-unset}"\n')
        spy.chmod(0o755)
        self.assertEqual(self.box.pnpm(['lint:fast']).stdout, b'depth=1\n')
        self.assertEqual(self.box.pnpm(['test:unit'], cwd=self.box.root).stdout, b'depth=1\n')

    def test_guard_is_exported_across_the_routed_fallback(self):
        self.box.reconfigure({'mode': 'unreachable'})
        spy = self.box.realbin / 'pnpm'
        spy.write_text('#!/bin/sh\necho "depth=${PANDORA_ROUTE_DEPTH:-unset}"\n')
        spy.chmod(0o755)
        self.assertEqual(self.box.pnpm(['test:unit']).stdout, b'depth=1\n')

    def test_shim_never_finds_itself_on_path(self):
        """PATH holding the shim dir twice must still resolve the real pnpm."""
        shim_dir = str(HERE / 'bin')
        path = os.pathsep.join([shim_dir, shim_dir, str(self.box.realbin), '/usr/bin', '/bin'])
        result = self.box.pnpm(['lint:fast'], PATH=path)
        self.assertEqual(result.stdout, b'REAL lint:fast\n')

    def test_missing_real_pnpm_is_a_clear_127(self):
        result = self.box.pnpm(['lint:fast'], PATH=os.pathsep.join([str(HERE / 'bin'), '/usr/bin']))
        self.assertEqual(result.returncode, 127)
        self.assertIn(b'no real pnpm', result.stderr)


class WorktreeEnrolment(unittest.TestCase):
    """Enrolment is per repository, so every worktree inherits it."""

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.start()

    def test_dot_git_file_in_a_worktree_resolves_to_the_common_dir(self):
        worktree = self.box.worktree('wt-a')
        self.assertEqual(enrolment.common_dir(worktree), str(self.box.repo / '.git'))

    def test_one_enrolment_covers_every_worktree(self):
        first, second = self.box.worktree('wt-a'), self.box.worktree('wt-b')
        self.box.enrol()                       # written once, into the common dir
        self.assertEqual(self.box.pnpm(['test:unit'], cwd=first).stdout, b'hello\n')
        self.assertEqual(self.box.pnpm(['test:unit'], cwd=second).stdout, b'hello\n')

    def test_a_worktree_created_after_enrolment_is_covered(self):
        self.box.enrol()
        late = self.box.worktree('wt-late')
        self.assertEqual(self.box.pnpm(['test:unit'], cwd=late).stdout, b'hello\n')

    def test_a_subdirectory_of_a_worktree_is_covered(self):
        worktree = self.box.worktree('wt-a')
        (worktree / 'apps' / 'desk').mkdir(parents=True)
        self.box.enrol()
        self.assertEqual(self.box.pnpm(['test:unit'], cwd=worktree / 'apps' / 'desk').stdout,
                         b'hello\n')

    def test_relative_gitdir_pointer_resolves(self):
        worktree = self.box.root / 'rel'
        worktree.mkdir()
        target = self.box.repo / '.git' / 'worktrees' / 'rel'
        target.mkdir(parents=True)
        (worktree / '.git').write_text('gitdir: ../repo/.git/worktrees/rel\n')
        self.box.enrol()
        self.assertEqual(self.box.pnpm(['test:unit'], cwd=worktree).stdout, b'hello\n')

    def test_unenrol_removes_the_claim_from_every_worktree(self):
        worktree = self.box.worktree('wt-a')
        self.box.enrol()
        (self.box.repo / '.git' / enrolment.MARKER).unlink()
        self.assertEqual(self.box.pnpm(['test:unit'], cwd=worktree).stdout, b'REAL test:unit\n')


class NestedReentry(unittest.TestCase):
    """The shim is re-entered by the repository's own scripts.

    Measured in eichler: ``pnpm check`` is ``pnpm validate check`` in the root
    package.json, and tools/validation/run.mjs spawns a bare ``pnpm`` again, so
    one typed command passes through this shim up to three times.  Only the
    outermost may route.
    """

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.start()
        self.box.enrol(claims=(('test:unit',), ('validate', 'unit')))

    def _nesting_real(self):
        """A 'real pnpm' that re-invokes pnpm, the way a package.json script does."""
        self.box.real.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "check" ]; then echo "script check -> pnpm validate unit";'
            ' exec pnpm validate unit; fi\n'
            'printf \'REAL %s\\n\' "$*"\n')
        self.box.real.chmod(0o755)

    def test_the_inner_pnpm_does_not_route(self):
        self._nesting_real()
        result = self.box.pnpm(['check'])
        self.assertIn(b'REAL validate unit', result.stdout)
        self.assertNotIn(b'hello', result.stdout)
        self.assertEqual(len(list((self.box.state / 'runs').glob('*/meta.json'))), 0)

    def test_the_outer_pnpm_still_routes_and_the_inner_one_does_not(self):
        self._nesting_real()
        result = self.box.pnpm(['test:unit'])
        self.assertEqual(result.stdout, b'hello\n')
        self.assertEqual(len(list((self.box.state / 'runs').glob('*/meta.json'))), 1)

    def test_three_levels_deep_still_routes_exactly_once(self):
        self.box.real.write_text(
            '#!/bin/sh\n'
            'case "$1" in\n'
            '  a) exec pnpm b ;;\n'
            '  b) exec pnpm test:unit ;;\n'
            'esac\n'
            'printf \'REAL %s\\n\' "$*"\n')
        self.box.real.chmod(0o755)
        result = self.box.pnpm(['a'])
        self.assertEqual(result.stdout, b'REAL test:unit\n')
        self.assertEqual(len(list((self.box.state / 'runs').glob('*/meta.json'))), 0)

    def test_an_env_scrubbing_hop_loses_the_guard(self):
        """The known hole: eichler's safeEnvironment() allowlist drops the marker.

        tools/validation/state.mjs keeps 16 names and PANDORA_ROUTE_DEPTH is not
        one of them, so anything spawned past `pueue add` starts guard-free.
        """
        self.box.real.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "submit" ]; then exec /usr/bin/env -i '
            '"PATH=$PATH" "HOME=$HOME" pnpm test:unit; fi\n'
            'printf \'REAL %s\\n\' "$*"\n')
        self.box.real.chmod(0o755)
        result = self.box.pnpm(['submit'])
        self.assertEqual(result.stdout, b'hello\n')       # it routed: the guard was lost
        self.assertEqual(len(list((self.box.state / 'runs').glob('*/meta.json'))), 1)


class PythonShimParity(unittest.TestCase):
    """The Python shim exists only to be measured; it must decide identically."""

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.start()
        self.box.enrol()

    def test_same_decisions(self):
        for args, expected in ((['test:unit'], b'hello\n'),
                               (['lint:fast'], b'REAL lint:fast\n'),
                               (['run', 'test:unit'], b'hello\n')):
            sh = self.box.pnpm(args)
            py = self.box.pnpm(args, shim='pnpm-python')
            self.assertEqual(sh.stdout, expected, args)
            self.assertEqual(py.stdout, expected, args)


if __name__ == '__main__':
    unittest.main()
