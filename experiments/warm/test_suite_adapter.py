"""Contract probes for the frozen suite adapter, without a Worker or Docker."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ADAPTER = Path(__file__).with_name('suite.mjs').resolve().as_uri()
DIGEST = 'a' * 64


def run_adapter(config, mocks):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        loader = root / 'loader.mjs'
        loader.write_text("""
const mocks = JSON.parse(process.env.PANDORA_TEST_MOCKS);
export async function resolve(specifier, context, nextResolve) {
  if (mocks[specifier]) return {url:'data:text/javascript,'+encodeURIComponent(mocks[specifier]),shortCircuit:true};
  return nextResolve(specifier, context);
}
""")
        driver = root / 'driver.mjs'
        driver.write_text("""
globalThis.events=[]; console.log=()=>{}; console.error=(...x)=>globalThis.events.push(['error',x.join(' ')]);
await import(process.env.PANDORA_TEST_ADAPTER);
process.stdout.write(JSON.stringify(globalThis.events));
""")
        result = subprocess.run(
            ['node', '--experimental-loader', str(loader), str(driver)], text=True, capture_output=True,
            env=os.environ | {'PANDORA_SUITE_CONFIG': json.dumps(config),
                 'PANDORA_TEST_MOCKS': json.dumps(mocks), 'PANDORA_TEST_ADAPTER': ADAPTER},
        )
        return result, json.loads(result.stdout)


class SuiteAdapterTests(unittest.TestCase):
    def test_plan_uses_catalog_cover_and_emits_canonical_plan(self):
        mocks = {
            '/workspace/source/packages/scenarios/src/journeys/index.ts': """
export const loadJourneys=async()=>[{id:'S0-01',consequential:true},{id:'S0-02',consequential:false}];""",
            '/workspace/source/packages/scenarios/src/cli/plan.ts': """
export const loadRouteManifest=async()=>({}); export const loadWeights=async()=>new Map([['S0-01',5]]);
export const coverJourneys=(c,m,w)=>{globalThis.events.push(['cover',c.map(x=>x.id)]);return new Set(['S0-01'])};
export const shardJourneys=(c,r,w,s)=>c;""",
            '/workspace/source/tools/stack/instance.mjs': "export const startInstance=async()=>{throw Error('must not start')};",
            'node:fs/promises': """
export const mkdir=async()=>{}; export const writeFile=async(p,v)=>globalThis.events.push(['write',p,JSON.parse(v)]);
export const readdir=async()=>[]; export const readFile=async()=>{throw Error('unused')}; export const rm=async()=>{};""",
        }
        result, events = run_adapter({'action':'plan','source_digest':DIGEST,'shard_count':1,'selection':None}, mocks)
        self.assertEqual(result.returncode, 0)
        self.assertIn(['cover', ['S0-01', 'S0-02']], events)
        plan = next(event[2] for event in events if event[0] == 'write' and event[1].endswith('suite-plan.json'))
        self.assertEqual(plan['catalog'], [{'id':'S0-01','consequential':True},{'id':'S0-02','consequential':False}])
        self.assertEqual(plan['replay_ids'], ['S0-01'])
        self.assertEqual(plan['shards'], [{'index':1,'ids':['S0-01','S0-02']}])
        self.assertEqual(len(plan['plan_id']), 64)

    def test_frozen_plan_mismatch_fails_before_stack_start(self):
        mocks = {
            '/workspace/source/packages/scenarios/src/journeys/index.ts': "export const loadJourneys=async()=>[];",
            '/workspace/source/packages/scenarios/src/cli/plan.ts': "export const loadRouteManifest=async()=>({}); export const loadWeights=async()=>new Map(); export const coverJourneys=()=>new Set(); export const shardJourneys=()=>[];",
            '/workspace/source/tools/stack/instance.mjs': "export const startInstance=async()=>{globalThis.events.push(['start']);return {}};",
            'node:fs/promises': "export const mkdir=async()=>{}; export const writeFile=async(p,v)=>globalThis.events.push(['write',p]); export const readdir=async()=>[]; export const readFile=async()=>''; export const rm=async()=>{};",
        }
        bad = {'version':1,'source_digest':DIGEST,'selection':None,'catalog':[{'id':'S0-01','consequential':True}], 'replay_ids':['S0-01'], 'shards':[{'index':1,'ids':['S0-01']}], 'plan_id':'b'*64}
        result, events = run_adapter({'action':'shard','source_digest':DIGEST,'plan':bad,'shard':1}, mocks)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(['start'], events)

    def test_shard_child_gets_only_suite_world_environment(self):
        source = Path(__file__).with_name('suite.mjs').read_text()
        self.assertIn("APP_API_URL: stack.api", source)
        self.assertIn("JOURNEY_SHARD: `${config.shard}/${shardCount}`", source)
        self.assertIn("JOURNEY_CONCURRENCY: '1'", source)
        self.assertIn("JOURNEY_REPLAY: 'cover'", source)
        self.assertIn('delete environment.JOURNEY_TEMPLATE', source)
        self.assertIn('delete environment.APP_WORLD', source)
        self.assertIn("'src/cli/journeys.ts', ...(update ? ['--update'] : [])", source)

    def test_update_uses_the_plural_cli_flag_and_emits_a_delta_receipt(self):
        source = Path(__file__).with_name('suite.mjs').read_text()
        self.assertIn("...(update ? ['--update'] : [])", source)
        self.assertIn('delete environment.CI', source)
        self.assertIn("await write('suite-update.json', updateProposal)", source)
        self.assertIn('Suite update modified an unowned route entry', source)
        self.assertIn('Suite update modified an unowned fixture', source)

    def test_missing_reports_are_an_infrastructure_failure(self):
        source = Path(__file__).with_name('suite.mjs').read_text()
        self.assertIn("Missing or malformed suite ${label} report", source)
        self.assertIn('unrunJourneys: [...shardContext.planned_ids]', source)
        self.assertIn("['results.json', 'errors.json', 'coverage.json']", source)
        self.assertIn("rm(`${JOURNEYS}/${name}`, { force: true })", source)


if __name__ == '__main__':
    unittest.main()
