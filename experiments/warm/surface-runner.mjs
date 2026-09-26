import { createHash } from 'node:crypto';
import { copyFileSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { spawnSync } from 'node:child_process';

const args = process.argv.slice(2);
const requestPath = args[args.indexOf('--request') + 1];
const sourceDigest = args[args.indexOf('--source-digest') + 1];
const parentAttempt = args[args.indexOf('--parent-attempt') + 1];
if (!requestPath || !sourceDigest || !parentAttempt) throw new Error('request, source digest, and parent attempt are required');
const request = JSON.parse(readFileSync(requestPath, 'utf8'));
const root = process.env.PANDORA_WORKSPACE_ROOT || '/workspace';
const source = join(root, 'source'); const results = join(root, 'results'); mkdirSync(results, { recursive: true });
const app = request.app || request.plan?.app;
const pkg = app === 'web' ? '@acme/web' : app === 'desk' ? '@acme/desk' : null;
if (!pkg) throw new Error('unsupported surface app');
const reporter = join(process.env.PANDORA_SURFACE_RUNNER_DIR || source, 'surface-reporter.cjs');
const asciiJSON = value => JSON.stringify(value).replace(/[^\x00-\x7f]/g, char => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`);
const canonical = value => Array.isArray(value) ? `[${value.map(canonical).join(',')}]` : value && typeof value === 'object' ? `{${Object.keys(value).sort().map(key => `${asciiJSON(key)}:${canonical(value[key])}`).join(',')}}` : asciiJSON(value);
const hash = value => createHash('sha256').update(canonical(value)).digest('hex');
const outputManifest = () => {
  const files = [];
  const visit = path => { for (const name of readdirSync(path)) { const item = join(path, name); const stat = lstatSync(item); if (stat.isSymbolicLink()) throw new Error('unsafe surface build output'); if (stat.isDirectory()) visit(item); else if (stat.isFile()) files.push({ path: relative(source, item), sha256: createHash('sha256').update(readFileSync(item)).digest('hex') }); else throw new Error('unsafe surface build output'); } };
  for (const path of [join(source, 'apps', app, 'dist'), join(source, 'apps', app, 'e2e', 'dist')]) { if (!existsSync(path) || lstatSync(path).isSymbolicLink() || !lstatSync(path).isDirectory()) throw new Error('missing generated surface output'); visit(path); }
  files.sort((a, b) => Buffer.from(a.path).compare(Buffer.from(b.path))); if (!files.length) throw new Error('empty generated surface output');
  const manifest = { app, files }; return { ...manifest, sha256: hash(manifest) };
};
const invoke = (extra, report, mode) => spawnSync('pnpm', ['--filter', pkg, 'exec', 'playwright', 'test', ...extra, '--workers=1', '--reporter=line,junit,' + reporter, '--output=' + join(results, 'playwright')], { cwd: source, env: { ...process.env, PLAYWRIGHT_JUNIT_OUTPUT_FILE: join(results, 'junit.xml'), PANDORA_SURFACE_REPORT: report, PANDORA_SURFACE_REPORT_MODE: mode }, stdio: 'inherit' });
const exitCode = result => result.error || result.status === null ? 70 : result.status;
const selectors = request.selectors || request.plan.selectors;
if (request.action === 'plan') {
  for (const build of [[], ['--mode', 'e2e']]) {
    const result = spawnSync('pnpm', ['--filter', pkg, 'exec', 'vite', 'build', ...build], { cwd: source, stdio: 'inherit' });
    if (exitCode(result)) process.exit(exitCode(result));
  }
  const fullReport = join(results, 'surface-list-full.json');
  const full = invoke([...selectors, '--list'], fullReport, 'list');
  if (exitCode(full)) process.exit(exitCode(full));
  const fullInventory = JSON.parse(readFileSync(fullReport, 'utf8')).inventory;
  const inventories = [];
  for (let index = 1; index <= request.shard_count; index++) {
    const report = join(results, `surface-list-${index}.json`);
    const result = invoke([...selectors, `--shard=${index}/${request.shard_count}`, '--list', '--pass-with-no-tests'], report, 'list');
    if (exitCode(result)) process.exit(exitCode(result));
    inventories.push(JSON.parse(readFileSync(report, 'utf8')).inventory);
  }
  const tests = inventories.flat();
  if (!tests.length) throw new Error('surface selection contains no tests');
  if (new Set(tests.map(x => x.id)).size !== tests.length || new Set(tests.map(x => x.id)).size !== fullInventory.length || !fullInventory.every(x => tests.some(y => y.id === x.id))) throw new Error('surface shard inventories do not partition the full selection');
  const plan = { version: 1, parent_attempt: parentAttempt, source_digest: sourceDigest, app, selectors, shard_count: request.shard_count, keep_going: request.keep_going, build: outputManifest(), tests, shards: inventories.map((rows, i) => ({ index: i + 1, test_ids: rows.map(x => x.id), inventory_sha256: hash(rows.map(x => x.id)) })) };
  plan.plan_id = hash(plan);
  writeFileSync(join(results, 'surface-plan.json'), JSON.stringify(plan, null, 2) + '\n');
  process.exit(0);
}
const plan = request.plan;
if (request.action !== 'shard' || plan.source_digest !== sourceDigest || plan.parent_attempt !== parentAttempt || plan.app !== app) throw new Error('surface shard request identity mismatch');
if (canonical(outputManifest()) !== canonical(plan.build)) throw new Error('surface build bytes differ from frozen plan');
const report = join(results, 'surface-observed.json'); const index = request.shard;
const result = invoke([...plan.selectors, `--shard=${index}/${plan.shard_count}`, '--pass-with-no-tests'], report, 'run');
const after = outputManifest();
const plannedFiles = new Map(plan.build.files.map(file => [file.path, file.sha256]));
const afterFiles = new Map(after.files.map(file => [file.path, file.sha256]));
if (plan.build.files.some(file => afterFiles.get(file.path) !== file.sha256)) {
  throw new Error('surface tests changed or removed a frozen compiled input');
}
for (const file of after.files) {
  if (plannedFiles.has(file.path)) continue;
  const destination = join(results, 'generated', file.path);
  mkdirSync(dirname(destination), { recursive: true });
  copyFileSync(join(source, file.path), destination);
}
const observed = JSON.parse(readFileSync(report, 'utf8'));
const expected = plan.shards[index - 1].test_ids;
if (JSON.stringify(observed.inventory.map(x => x.id)) !== JSON.stringify(expected)) throw new Error('surface shard membership changed');
const outcomes = observed.outcomes;
if (new Set(outcomes.map(x => x.id)).size !== expected.length || outcomes.some(x => !expected.includes(x.id))) throw new Error('surface shard outcomes changed');
const code = exitCode(result);
writeFileSync(join(results, 'surface-shard.json'), JSON.stringify({ version: 1, plan_id: plan.plan_id, parent_attempt: parentAttempt, source_digest: sourceDigest, app, shard: index, planned_ids: expected, observed_ids: observed.inventory.map(x => x.id), outcomes, exit_code: code, detail: observed.status || '' }, null, 2) + '\n');
process.exit(code);
