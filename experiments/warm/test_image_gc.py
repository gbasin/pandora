import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from docker_images import publish, reserve, remove
from image_gc import collect


class ImageGCTests(unittest.TestCase):
    def test_reservation_survives_rebuild_removal_and_retention(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            built = root / 'runs' / ('a' * 32)
            built.mkdir(parents=True)
            (built / 'submission.json').write_text(json.dumps({'docker': {'request': {'kind': 'build'}}}))
            (built / 'terminal.json').write_text('{"cleanup_verified":true}')
            (built / 'released').touch()
            old = {'image_id': 'sha256:old', 'attempt': built.name}
            publish(root, 'a' * 64, 'app:test', old)
            reserved = reserve(root, 'a' * 64, 'app:test', 'b' * 32)
            publish(root, 'a' * 64, 'app:test', {'image_id': 'sha256:new'})
            remove(root, 'a' * 64, 'app:test')
            self.assertEqual(reserve(root, 'a' * 64, 'app:test', 'b' * 32), reserved)
            calls = []
            def docker(argv, **kwargs):
                calls.append(argv)
                if 'ls' in argv:
                    return subprocess.CompletedProcess(argv, 0, json.dumps({
                        'Repository': 'pandora-build', 'Tag': built.name, 'ID': 'sha256:old'}) + '\n')
                return subprocess.CompletedProcess(argv, 0, '')
            with patch('image_gc.subprocess.run', side_effect=docker):
                self.assertEqual(collect(root), [])
                shutil.rmtree(built)  # ledger preserves eligibility after attempt retention
                run = root / 'runs' / ('b' * 32)
                run.mkdir()
                (run / 'terminal.json').write_text('{"cleanup_verified":true}')
                self.assertEqual(collect(root), [])  # result still unacknowledged
                (run / 'released').touch()
                self.assertEqual(collect(root), ['pandora-build:' + 'a' * 32])
            self.assertEqual(sum('rm' in call for call in calls), 1)

    def test_current_mapping_and_unacknowledged_build_are_never_collected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'runs').mkdir()
            (root / 'docker-collectible.json').write_text(json.dumps(['pandora-build:' + 'a' * 32]))
            publish(root, 'a' * 64, 'app:test', {'image_id': 'sha256:current'})
            listing = '\n'.join(json.dumps({'Repository': repo, 'Tag': tag, 'ID': identity})
                                for repo, tag, identity in [('pandora-build', 'a' * 32, 'sha256:current'),
                                                          ('pandora-build', 'b' * 32, 'sha256:unresolved'),
                                                          ('foreign', 'latest', 'sha256:foreign')])
            with patch('image_gc.subprocess.run', return_value=subprocess.CompletedProcess([], 0, listing)) as call:
                self.assertEqual(collect(root), [])
                self.assertEqual(call.call_count, 1)

    def test_incomplete_pin_defers_collection_without_dropping_images(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'docker-pins').mkdir()
            (root / 'docker-pins' / ('a' * 32 + '.json')).write_text('{')
            with patch('image_gc.subprocess.run') as call:
                self.assertEqual(collect(root), [])
                call.assert_not_called()

    def test_concurrent_partial_submission_defers_collection_until_metadata_is_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'runs').mkdir()
            tag = 'pandora-build:' + 'a' * 32
            (root / 'docker-collectible.json').write_text(json.dumps([tag]))
            transferring = root / 'runs' / ('b' * 32); transferring.mkdir()
            submission = transferring / 'submission.json'
            opened, finish = threading.Event(), threading.Event()
            def transfer():
                submission.write_text('')
                opened.set(); finish.wait(3)
                submission.write_text(json.dumps({'docker': {'image': {'image_id': 'sha256:protected'}}}))
            writer = threading.Thread(target=transfer)
            writer.start(); self.assertTrue(opened.wait(3))
            listing = json.dumps({'Repository': 'pandora-build', 'Tag': 'a' * 32, 'ID': 'sha256:protected'}) + '\n'
            with patch('image_gc.subprocess.run', return_value=subprocess.CompletedProcess([], 0, listing)) as docker:
                self.assertEqual(collect(root), [])
                docker.assert_not_called()
                finish.set(); writer.join(3)
                self.assertFalse(writer.is_alive())
                self.assertEqual(collect(root), [])
                self.assertEqual(docker.call_count, 1)


if __name__ == '__main__':
    unittest.main()
