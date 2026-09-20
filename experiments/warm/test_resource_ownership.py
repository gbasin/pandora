import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from resource_ownership import check, OwnershipUnresolved


class ResourceOwnership(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ids = ['a' * 32, 'b' * 32]
        self.locks = []
        for identity, kind in zip(self.ids, ('journey', 'docker')):
            attempt = self.root / 'runs' / identity
            attempt.mkdir(parents=True)
            (attempt / 'submission.json').write_text(json.dumps({'workflow': kind}))
            (attempt / ('service-cleanup.pending' if kind == 'journey' else 'docker-cleanup.pending')).touch()
            handle = (attempt / 'attempt.lock').open('a')
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.addCleanup(handle.close)
            self.locks.append(handle)
        self.containers = [{'Names': 'pandora-warm-' + identity, 'State': 'running',
                            'Labels': 'pandora.attempt=' + identity + ',pandora.workflow=' + kind}
                           for identity, kind in zip(self.ids, ('journey', 'docker'))]
        self.networks = [{'Name': 'pandora-warm-' + self.ids[0], 'Labels': 'pandora.attempt=' + self.ids[0]}]

    def verify(self, admitted=None):
        with patch('resource_ownership.inventory', side_effect=lambda args: self.containers if args == ['ps', '-a'] else self.networks):
            check(self.root, set(self.ids) if admitted is None else admitted)

    def test_two_admitted_live_workflows_and_owned_network_are_accepted(self):
        self.verify()

    def test_admission_refresh_includes_peer_that_arrived_during_inventory(self):
        calls = []
        def current():
            calls.append(True)
            return {self.ids[0]} if len(calls) == 1 else set(self.ids)
        self.verify(current)
        self.assertEqual(len(calls), 2)

    def test_live_but_unadmitted_peer_remains_blocked_in_production(self):
        with self.assertRaisesRegex(OwnershipUnresolved, 'not admitted'):
            self.verify({self.ids[0]})

    def test_dead_peer_with_leftover_container_blocks_even_with_stale_admission_snapshot(self):
        self.locks[1].close()
        with self.assertRaisesRegex(OwnershipUnresolved, 'dead'):
            self.verify()

    def test_mismatched_label_or_workflow_is_not_ownership(self):
        self.containers[1]['Labels'] = 'pandora.attempt=' + self.ids[0] + ',pandora.workflow=docker'
        with self.assertRaisesRegex(OwnershipUnresolved, 'disagree'):
            self.verify()
        self.containers[1]['Labels'] = 'pandora.attempt=' + self.ids[1] + ',pandora.workflow=journey'
        with self.assertRaisesRegex(OwnershipUnresolved, 'submission'):
            self.verify()

    def test_orphan_network_and_missing_intent_block(self):
        self.networks[0]['Labels'] = 'pandora.attempt=' + self.ids[1]
        with self.assertRaisesRegex(OwnershipUnresolved, 'Network name'):
            self.verify()
        self.networks.clear()
        (self.root / 'runs' / self.ids[0] / 'service-cleanup.pending').unlink()
        with self.assertRaisesRegex(OwnershipUnresolved, 'intent'):
            self.verify()

    def test_legacy_stopped_surface_diagnostics_remain_allowed(self):
        self.containers.append({'Names': 'pandora-warm-' + 'c' * 32, 'State': 'exited', 'Labels': 'pandora.experiment=warm-surface'})
        self.verify()
        self.containers[-1]['State'] = 'running'
        with self.assertRaises(OwnershipUnresolved):
            self.verify()

    def test_peer_finishing_after_inventory_does_not_cause_false_orphan(self):
        self.locks[1].close()
        with patch('resource_ownership.inventory', side_effect=[self.containers, self.networks, self.containers[:1], self.networks]) as query:
            check(self.root, set(self.ids))
        self.assertEqual(query.call_count, 4)

    def test_active_builder_requires_recorded_admitted_owner_and_held_builder_lock(self):
        builder = 'pandora-surface-deps-v3'
        directory = self.root / 'builder-owners'
        directory.mkdir()
        (directory / (builder + '.json')).write_text(json.dumps({'attempt': self.ids[0]}))
        (self.root / 'runs' / self.ids[0] / 'dependency-cleanup.pending').touch()
        self.containers.append({'Names': 'buildx_buildkit_' + builder + '0', 'State': 'running', 'Labels': ''})
        with self.assertRaisesRegex(OwnershipUnresolved, 'lease'):
            self.verify()
        with (directory / (builder + '.lock')).open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.verify()
            (directory / (builder + '.json')).unlink()
            with self.assertRaisesRegex(OwnershipUnresolved, 'no recorded owner'):
                self.verify()

    def test_new_surface_labels_require_admitted_live_owner(self):
        identity = self.ids[1]
        (self.root / 'runs' / identity / 'submission.json').write_text('{"workflow":"surface"}')
        self.containers[1]['Labels'] = 'pandora.attempt=' + identity + ',pandora.workflow=surface,pandora.experiment=warm-surface'
        self.verify()
        with self.assertRaises(OwnershipUnresolved):
            self.verify({self.ids[0]})
        self.locks[1].close()
        with self.assertRaises(OwnershipUnresolved):
            self.verify()

    def test_docker_container_without_cleanup_intent_blocks(self):
        (self.root / 'runs' / self.ids[1] / 'docker-cleanup.pending').unlink()
        with self.assertRaisesRegex(OwnershipUnresolved, 'intent'):
            self.verify()

    def test_docker_run_cannot_own_buildkit(self):
        identity = self.ids[1]
        builder = 'pandora-docker-builds-v1'
        (self.root / 'runs' / identity / 'submission.json').write_text('{"workflow":"docker","docker":{"request":{"kind":"run"}}}')
        directory = self.root / 'builder-owners'
        directory.mkdir()
        (directory / (builder + '.json')).write_text(json.dumps({'attempt': identity}))
        self.containers.append({'Names': 'buildx_buildkit_' + builder + '0', 'State': 'running', 'Labels': ''})
        with (directory / (builder + '.lock')).open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with self.assertRaisesRegex(OwnershipUnresolved, 'incompatible workflow'):
                self.verify()

    def test_unknown_managed_container_blocks_but_unrelated_container_does_not(self):
        self.containers.append({'Names': 'unrelated', 'State': 'running', 'Labels': ''})
        self.verify()
        self.containers[-1]['Labels'] = 'pandora.unknown=true'
        with self.assertRaisesRegex(OwnershipUnresolved, 'Unrecognized'):
            self.verify()
        self.containers[-1]['Labels'] = ''
        self.containers[-1]['Names'] = 'pandora-warm-unrecognized'
        with self.assertRaisesRegex(OwnershipUnresolved, 'Unexpected'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
