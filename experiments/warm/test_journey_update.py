import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from journey import journey_command, journey_config, mark_cleanup_failure


class JourneyUpdateTests(unittest.TestCase):
    def test_only_the_focused_selector_shapes_are_accepted(self):
        self.assertEqual(journey_config({'selectors': ['S0-01']}), {'id': 'S0-01', 'update': False})
        self.assertEqual(journey_config({'selectors': ['S0-01', '--update']}), {'id': 'S0-01', 'update': True})
        for selectors in ([], ['S0-02'], ['S0-01', '--update', 'extra'], ['--update', 'S0-01']):
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                journey_config({'selectors': selectors})

    def test_update_clears_ci_only_for_the_journey_child(self):
        normal = journey_command({'id': 'S0-01', 'update': False, 'attempt': 'a' * 32})
        update = journey_command({'id': 'S0-01', 'update': True, 'attempt': 'a' * 32})
        self.assertNotIn('env', normal)
        self.assertEqual(update[-7:-4], ['env', '-u', 'CI'])
        self.assertIn('PANDORA_JOURNEY_CONFIG={"id":"S0-01","update":true,"attempt":"' + 'a' * 32 + '"}', update)

    def test_cleanup_failure_overrides_a_passing_report_without_losing_proposals(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp)
            results = attempt / 'results'
            results.mkdir()
            original = {
                'journey': 'S0-01', 'status': 'pass', 'detail': '', 'update': True,
                'proposals': [{'path': 'updates/packages/scenarios/fixtures/S0-01.ledger.jsonl', 'sha256': 'a' * 64}],
            }
            (results / 'journey.json').write_text(json.dumps(original))
            mark_cleanup_failure(attempt)
            report = json.loads((results / 'journey.json').read_text())
            self.assertEqual(report['status'], 'fail')
            self.assertTrue(report['update'])
            self.assertEqual(report['proposals'], original['proposals'])
            self.assertIn('cleanup failed', report['detail'])

    def test_adapter_passes_update_to_the_runner_and_returns_declared_proposals(self):
        adapter = Path(__file__).with_name('journey.mjs').resolve().as_uri()
        mocks = {
            '/workspace/source/tools/stack/instance.mjs': """
                export const startInstance = async () => ({ api: 'http://api', secrets: [], stop: async () => {} });
            """,
            '/workspace/source/packages/scenarios/src/journeys/index.ts': """
                export const loadJourneys = async () => [{ id: 'S0-01' }];
            """,
            '/workspace/source/packages/scenarios/src/runner.ts': """
                export const runJourney = async (_journey, options) => {
                  globalThis.events.push({ kind: 'run', options });
                  return { id: 'S0-01', status: 'pass', detail: '', writeRoutes: { route: 'value' } };
                };
            """,
            '/workspace/source/packages/scenarios/src/cli/plan.ts': """
                export const checkRoutes = async (_id, _routes, update) => globalThis.events.push({ kind: 'routes', update });
            """,
            'node:fs/promises': """
                export const mkdir = async () => {};
                export const copyFile = async (source, destination) => globalThis.events.push({ kind: 'copy', source, destination });
                export const readFile = async () => Buffer.from('proposal');
                export const writeFile = async (path, contents) => globalThis.events.push({ kind: 'report', path, report: JSON.parse(contents) });
            """,
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            loader = root / 'loader.mjs'
            loader.write_text("""
                const mocks = JSON.parse(process.env.PANDORA_TEST_MOCKS);
                export async function resolve(specifier, context, nextResolve) {
                  if (mocks[specifier]) return { url: 'data:text/javascript,' + encodeURIComponent(mocks[specifier]), shortCircuit: true };
                  return nextResolve(specifier, context);
                }
            """)
            driver = root / 'driver.mjs'
            driver.write_text("""
                globalThis.events = [];
                console.log = () => {};
                await import(process.env.PANDORA_TEST_ADAPTER);
                process.stdout.write(JSON.stringify(globalThis.events));
            """)
            for update in (False, True):
                environment = os.environ | {
                    'PANDORA_JOURNEY_CONFIG': json.dumps({'id': 'S0-01', 'update': update}),
                    'PANDORA_TEST_ADAPTER': adapter,
                    'PANDORA_TEST_MOCKS': json.dumps(mocks),
                }
                completed = subprocess.run(
                    ['node', '--experimental-loader', str(loader), str(driver)],
                    check=True, capture_output=True, text=True, env=environment,
                )
                events = json.loads(completed.stdout)
                run = next(event for event in events if event['kind'] == 'run')
                routes = next(event for event in events if event['kind'] == 'routes')
                report = next(event['report'] for event in events if event['kind'] == 'report')
                self.assertEqual(run['options']['update'], update)
                self.assertFalse(run['options']['printPrincipals'])
                self.assertEqual(routes['update'], update)
                self.assertEqual(report['update'], update)
                if update:
                    self.assertEqual(
                        [proposal['path'] for proposal in report['proposals']],
                        ['updates/packages/scenarios/fixtures/S0-01.ledger.jsonl',
                         'updates/packages/scenarios/fixtures/write-routes.json'],
                    )
                    self.assertTrue(all(len(proposal['sha256']) == 64 for proposal in report['proposals']))
                else:
                    self.assertEqual(report['proposals'], [])


if __name__ == '__main__':
    unittest.main()
