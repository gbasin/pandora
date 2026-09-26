import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from surface_child import build_source, request, validate_result
from surface_suite import outputs_manifest, plan_digest
from test_surface_suite import plan, report

class SurfaceChildTests(unittest.TestCase):
    def metadata(self, value, index=1):
        return {'attempt': 'c' * 32, 'workflow': 'surface', 'parent_attempt': value['parent_attempt'],
                'source_digest': value['source_digest'], 'surface_app': value['app'],
                'selectors': value['selectors'], 'surface_suite': {'action': 'shard', 'plan': value, 'shard': index}}

    def test_child_cannot_substitute_app_source_or_selectors(self):
        value = plan()
        for field, replacement in (('source_digest', 'f' * 64), ('surface_app', 'desk'), ('selectors', []), ('parent_attempt', 'f' * 32)):
            metadata = self.metadata(value); metadata[field] = replacement
            with self.subTest(field=field), self.assertRaises(ValueError): request(metadata)

    def test_result_exit_must_match_verified_report(self):
        value = plan(); metadata = self.metadata(value)
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory); (stage / 'results').mkdir()
            (stage / 'results/surface-shard.json').write_text(json.dumps(report(value, 1)))
            with self.assertRaises(ValueError):
                validate_result(stage, metadata, {'exit_code': 70}, {'results/surface-shard.json': ''})
            validate_result(stage, metadata, {'exit_code': 0}, {'results/surface-shard.json': ''})

    def test_build_resolution_checks_reserved_child_and_manifest(self):
        value = plan(); metadata = self.metadata(value)
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory); attempt = runs / metadata['attempt']; attempt.mkdir()
            parent = runs / value['parent_attempt']; parent.mkdir()
            children = ['d' * 32, attempt.name, 'e' * 32, 'f' * 32]
            (parent / 'children.json').write_text(json.dumps({'version': 1, 'parent_attempt': parent.name, 'children': children}))
            planner = parent / 'results/attempts' / children[0]
            source = planner / 'results/outputs'
            for name in ('apps/web/dist/a.js', 'apps/web/e2e/dist/b.js'):
                path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(name)
            value['build'] = outputs_manifest(source, value['app']); value['plan_id'] = plan_digest(value)
            planner_meta = metadata | {'attempt': children[0], 'surface_suite': {key: value[key] for key in ('app', 'selectors', 'shard_count', 'keep_going')} | {'action': 'plan'}}
            (planner / 'submission.json').write_text(json.dumps(planner_meta))
            (planner / 'results/surface-plan.json').write_text(json.dumps(value))
            with patch('surface_child.validate_evidence', return_value={'exit_code': 0}):
                self.assertEqual(build_source(attempt, metadata), source)
                (source / 'apps/web/dist/a.js').write_text('different build')
                with self.assertRaises(ValueError): build_source(attempt, metadata)
            foreign = runs / ('9' * 32); foreign.mkdir()
            with self.assertRaises(ValueError): build_source(foreign, metadata)

if __name__ == '__main__': unittest.main()
