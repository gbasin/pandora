/**
 * Frozen-catalog journey-suite adapter.  Planning is pure; a shard starts one
 * Workers runtime and lets Eichler's own plural CLI create and dispose worlds.
 */
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { readdir, readFile, writeFile, mkdir, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { startInstance } from '/workspace/source/tools/stack/instance.mjs';
import { loadJourneys } from '/workspace/source/packages/scenarios/src/journeys/index.ts';
import { loadRouteManifest, loadWeights, coverJourneys, shardJourneys } from '/workspace/source/packages/scenarios/src/cli/plan.ts';

const SOURCE = '/workspace/source';
const RESULTS = '/workspace/results';
const JOURNEYS = `${SOURCE}/packages/scenarios/.journeys`;
const idPattern = /^(?:S[0-6]|SX)-\d{2}$/;
const digestPattern = /^[a-f0-9]{64}$/;

function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  if (value && typeof value === 'object')
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
}
function hash(value) { return createHash('sha256').update(stable(value)).digest('hex'); }
function fail(message) { throw new Error(message); }
function parseConfig() {
  const raw = process.env.PANDORA_SUITE_CONFIG;
  if (!raw) fail('Pandora suite configuration is required');
  let config;
  try { config = JSON.parse(raw); } catch { fail('Invalid Pandora suite configuration JSON'); }
  if (!config || typeof config !== 'object' || !['plan', 'shard'].includes(config.action) || !digestPattern.test(config.source_digest))
    fail('Unsupported Pandora suite configuration');
  return config;
}
function selectionOf(value) {
  if (value === null) return null;
  if (!Array.isArray(value) || !value.length || value.some((id) => typeof id !== 'string' || !idPattern.test(id)) || new Set(value).size !== value.length)
    fail('Suite selection must be null or a unique nonempty list of journey IDs');
  return value;
}
function countOf(value) {
  if (!Number.isInteger(value) || value < 1 || value > 32) fail('Suite shard_count must be an integer from 1 through 32');
  return value;
}
async function buildPlan(sourceDigest, selection, shardCount) {
  const loaded = await loadJourneys();
  const byId = new Map(loaded.map((journey) => [journey.id, journey]));
  if (selection?.some((id) => !byId.has(id))) fail('Suite selection contains an ID absent from this snapshot');
  const catalog = loaded.filter((journey) => selection === null || selection.includes(journey.id));
  if (!catalog.length) fail('No journeys match the selected catalog');
  const manifest = await loadRouteManifest();
  const weights = await loadWeights(catalog.map((journey) => journey.id));
  const consequential = new Set(catalog.filter((journey) => journey.consequential).map((journey) => journey.id));
  // Preserve the product CLI's missing-manifest behavior exactly.
  const cover = coverJourneys(catalog, manifest, weights);
  const replayed = new Set([...cover, ...[...consequential].filter((id) => !(id in manifest))]);
  const shards = Array.from({ length: shardCount }, (_, index) => {
    const ids = shardJourneys(catalog, replayed, weights, `${index + 1}/${shardCount}`).map((journey) => journey.id);
    if (!ids.length) fail(`Suite shard ${index + 1}/${shardCount} is empty`);
    return { index: index + 1, ids };
  });
  const base = {
    version: 1,
    source_digest: sourceDigest,
    selection,
    catalog: catalog.map(({ id, consequential }) => ({ id, consequential })),
    replay_ids: catalog.filter((journey) => replayed.has(journey.id)).map((journey) => journey.id),
    shards,
  };
  return { ...base, plan_id: hash(base) };
}
function validPlan(plan) {
  if (!plan || typeof plan !== 'object' || plan.version !== 1 || !digestPattern.test(plan.source_digest) || !digestPattern.test(plan.plan_id))
    fail('Invalid frozen suite plan');
  const base = { ...plan }; delete base.plan_id;
  if (hash(base) !== plan.plan_id) fail('Frozen suite plan_id does not match its contents');
  const selection = selectionOf(plan.selection);
  if (!Array.isArray(plan.shards) || !plan.shards.length || plan.shards.length > 32) fail('Invalid frozen suite plan shards');
  if (!Array.isArray(plan.catalog) || !plan.catalog.length || !Array.isArray(plan.replay_ids)) fail('Invalid frozen suite plan catalog');
  return { selection, shardCount: plan.shards.length };
}
function exactFilter(selection) {
  return selection === null ? undefined : `^(?:${selection.map((id) => id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})$`;
}
async function fixtureState() {
  const directory = `${SOURCE}/packages/scenarios/fixtures`;
  const names = (await readdir(directory)).filter((name) => name === 'write-routes.json' || name.endsWith('.ledger.jsonl')).sort();
  const state = {};
  for (const name of names) state[name] = createHash('sha256').update(await readFile(join(directory, name))).digest('hex');
  return state;
}
function redactor(stack) {
  return (text) => {
    let result = String(text);
    for (const secret of stack?.secrets ?? []) result = result.split(secret).join('[redacted]');
    return result;
  };
}
async function runCli(env, redact) {
  const child = spawn(process.execPath, ['--import', 'tsx', 'src/cli/journeys.ts'], {
    cwd: `${SOURCE}/packages/scenarios`, env, stdio: ['ignore', 'pipe', 'pipe'],
  });
  const log = (stream) => createInterface({ input: stream }).on('line', (line) => console.log(redact(line)));
  log(child.stdout); log(child.stderr);
  return await new Promise((resolve) => {
    let settled = false;
    const done = (value) => { if (!settled) { settled = true; resolve(value); } };
    child.once('error', (error) => { console.error(redact(error.message)); done(1); });
    child.once('close', (code) => done(code ?? 1));
  });
}
async function json(path, label) {
  try { return JSON.parse(await readFile(path, 'utf8')); }
  catch { fail(`Missing or malformed suite ${label} report`); }
}
async function write(name, value) {
  await mkdir(RESULTS, { recursive: true });
  await writeFile(`${RESULTS}/${name}`, JSON.stringify(value, null, 2) + '\n');
}

let status = 1;
let config;
let stack;
let shardContext;
let shardReport;
try {
  config = parseConfig();
  if (config.action === 'plan') {
    const selection = selectionOf(config.selection);
    const plan = await buildPlan(config.source_digest, selection, countOf(config.shard_count));
    await write('suite-plan.json', plan);
    status = 0;
  } else {
    const frozen = config.plan;
    const { selection, shardCount } = validPlan(frozen);
    if (frozen.source_digest !== config.source_digest) fail('Frozen suite plan source digest differs from request');
    if (!Number.isInteger(config.shard) || config.shard < 1 || config.shard > shardCount) fail('Suite shard is outside the frozen plan');
    const recomputed = await buildPlan(config.source_digest, selection, shardCount);
    if (stable(recomputed) !== stable(frozen)) fail('Frozen suite plan does not match this snapshot');
    const planned = frozen.shards.find((entry) => entry.index === config.shard)?.ids;
    if (!Array.isArray(planned) || !planned.length) fail('Frozen suite shard is empty');
    shardContext = { plan_id: frozen.plan_id, source_digest: config.source_digest, shard: config.shard, planned_ids: planned };
    const before = await fixtureState();
    const controller = new AbortController();
    process.once('SIGTERM', () => controller.abort()); process.once('SIGINT', () => controller.abort());
    stack = await startInstance({ external: true, signal: controller.signal, output: () => {} });
    const environment = { ...process.env, IKE_API_URL: stack.api, JOURNEY_SHARD: `${config.shard}/${shardCount}`, JOURNEY_CONCURRENCY: '1', JOURNEY_REPLAY: 'cover' };
    const filter = exactFilter(selection);
    if (filter) environment.JOURNEY_FILTER = filter; else delete environment.JOURNEY_FILTER;
    delete environment.JOURNEY_TEMPLATE; delete environment.IKE_WORLD;
    // A frozen snapshot can force-include ignored artifacts. Never mistake an
    // earlier CLI's reports for this shard after a child startup failure.
    await Promise.all(['results.json', 'errors.json', 'coverage.json'].map((name) => rm(`${JOURNEYS}/${name}`, { force: true })));
    const code = await runCli(environment, redactor(stack));
    if (stack.crashed) fail(`Workers runtime exited unexpectedly: ${stack.crashed}`);
    const results = await json(`${JOURNEYS}/results.json`, 'results');
    const errors = await json(`${JOURNEYS}/errors.json`, 'errors');
    const coverage = await json(`${JOURNEYS}/coverage.json`, 'coverage');
    const after = await fixtureState();
    if (stable(before) !== stable(after)) fail('Readonly suite mutated journey fixtures');
    const seen = new Set(Array.isArray(results) ? results.map((result) => result?.id).filter((id) => typeof id === 'string') : []);
    const reported = Array.isArray(errors?.unrunJourneys) ? errors.unrunJourneys : [];
    const missing = [...new Set([...reported, ...planned.filter((id) => !seen.has(id))])].sort();
    shardReport = { version: 1, ...shardContext, exit_code: code, results, errors: { infrastructureFailures: errors?.infrastructureFailures, unrunJourneys: missing }, coverage, detail: missing.length ? `Suite did not report planned journeys: ${missing.join(', ')}` : code === 0 ? '' : 'Eichler suite CLI failed' };
    status = code === 0 && !missing.length ? 0 : 1;
  }
} catch (error) {
  const detail = redactor(stack)(error instanceof Error ? error.message : String(error));
  console.error(detail);
  if (shardContext && !shardReport)
    shardReport = { version: 1, ...shardContext, exit_code: 1, results: [], errors: { infrastructureFailures: 1, unrunJourneys: [...shardContext.planned_ids] }, coverage: null, detail };
  status = 1;
} finally {
  if (stack) {
    try { await stack.stop(); }
    catch (error) {
      const detail = redactor(stack)(error instanceof Error ? error.message : String(error));
      console.error(detail);
      if (shardReport) {
        shardReport.errors.infrastructureFailures = Number(shardReport.errors.infrastructureFailures ?? 0) + 1;
        shardReport.detail = shardReport.detail ? `${shardReport.detail}\nStack cleanup failed: ${detail}` : `Stack cleanup failed: ${detail}`;
      }
      status = 1;
    }
  }
  if (shardReport) {
    shardReport.exit_code = status;
    if (status && !shardReport.detail) shardReport.detail = 'Suite cleanup failed';
    await write('suite-shard.json', shardReport);
  }
  await mkdir(RESULTS, { recursive: true });
  await writeFile(`${RESULTS}/exit-code`, `${status}\n`);
}
process.exitCode = status;
