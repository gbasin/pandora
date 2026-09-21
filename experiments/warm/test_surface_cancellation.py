import json
from pathlib import Path
import tempfile
import unittest

from evidence import validate_evidence
from snapshot import digest
from surface_cancellation import write_receipt


PARENT = 'a' * 32
SOURCE = 'b' * 64


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class SurfaceCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.stage = Path(self.temp.name) / PARENT; self.stage.mkdir()
        self.submitted = {'attempt': PARENT, 'workflow': 'surface-run', 'source_digest': SOURCE}

    def terminal(self, cleanup=True):
        return {'attempt': PARENT, 'workflow': 'surface-run', 'exit_code': 130,
                'cleanup_verified': cleanup}

    def evidence(self, cleanup=True):
        terminal = self.terminal(cleanup)
        write(self.stage / 'submission.json', self.submitted)
        write(self.stage / 'terminal.json', terminal)
        manifest = {str(path.relative_to(self.stage)): digest(path) for path in self.stage.rglob('*')
                    if path.is_file() and path.name not in ('terminal.json', 'artifacts.json', 'submission.json')}
        write(self.stage / 'artifacts.json', manifest)
        return terminal

    def registry(self, children):
        write(self.stage / 'children.json', {'version': 1, 'parent_attempt': PARENT, 'children': children})

    def test_cancel_before_parent_dispatch_has_an_empty_registry_receipt(self):
        self.assertEqual(write_receipt(self.stage, self.submitted, True)['children'], [])
        self.evidence()
        self.assertEqual(validate_evidence(self.stage, PARENT)['exit_code'], 130)

    def test_cancel_during_planner_binds_the_reserved_registry(self):
        children = ['c' * 32, 'd' * 32]
        self.registry(children)
        self.assertEqual(write_receipt(self.stage, self.submitted, True)['children'], children)
        self.evidence()
        validate_evidence(self.stage, PARENT)

    def test_cancel_during_parallel_dispatch_binds_every_reserved_child(self):
        children = ['c' * 32, 'd' * 32, 'e' * 32, 'f' * 32]
        self.registry(children)
        write_receipt(self.stage, self.submitted, True)
        self.evidence()
        validate_evidence(self.stage, PARENT)

    def test_cleanup_not_verified_never_emits_a_cancellation_receipt(self):
        self.assertIsNone(write_receipt(self.stage, self.submitted, False))
        self.evidence(cleanup=False)
        with self.assertRaises(ValueError):
            validate_evidence(self.stage, PARENT)

    def test_forged_identity_or_source_is_rejected(self):
        write_receipt(self.stage, self.submitted, True)
        path = self.stage / 'results/surface-cancelled.json'
        value = json.loads(path.read_text()); value['parent_attempt'] = 'c' * 32; write(path, value)
        self.evidence()
        with self.assertRaises(ValueError): validate_evidence(self.stage, PARENT)
        value['parent_attempt'] = PARENT; value['source_digest'] = 'c' * 64; write(path, value)
        self.evidence()
        with self.assertRaises(ValueError): validate_evidence(self.stage, PARENT)

    def test_mismatched_registry_is_rejected(self):
        self.registry(['c' * 32, 'd' * 32])
        write_receipt(self.stage, self.submitted, True)
        self.registry(['c' * 32, 'e' * 32])
        self.evidence()
        with self.assertRaises(ValueError): validate_evidence(self.stage, PARENT)


if __name__ == '__main__': unittest.main()
