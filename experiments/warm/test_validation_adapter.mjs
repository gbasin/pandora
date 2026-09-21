import assert from 'node:assert/strict';
import test from 'node:test';
import { chmod, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import {
  browserCount,
  consoleCount,
  fullCommands,
  postgresCount,
  runValidation,
  tapCount,
} from './validation.mjs';

test('counts Node TAP passes and Jest summaries from runner output', () => {
  assert.equal(tapCount('# pass 2\n@eichler/brief:test: ℹ pass 3\n'), 5);
  assert.equal(consoleCount('Tests:       4 passed, 4 total\n'), 4);
  assert.equal(browserCount('  3 passed (2.3s)\n'), 3);
  assert.equal(postgresCount('Postgres invariants passed.\n'), 1);
});

const request = { version: 1, suite: 'unit', args: [], attempt: 'a'.repeat(32) };
const noop = async () => ({ files: [], omitted: [] });
const stable = () => 'stable';

async function fixture(name, contents) {
  const root = await mkdtemp(join(tmpdir(), 'pandora-validation-'));
  const results = join(root, 'results');
  const path = join(root, name);
  await writeFile(path, contents);
  return { root, results, path };
}

test('rejects a successful Node runner that executed only skipped tests and stops services', async () => {
  const { root, results, path } = await fixture(
    'skipped.test.mjs',
    "import test from 'node:test'; test.skip('not evidence', () => {});\n",
  );
  let stopped = false;
  const code = await runValidation({
    root,
    directory: results,
    request,
    planner: () => ({ commands: [['node', '--test', path]], env: {} }),
    initializeGit: async () => {},
    fingerprintSource: stable,
    collect: noop,
    service: { stack: { stop: async () => (stopped = true) } },
  });
  const receipt = JSON.parse(await readFile(join(results, 'validation.json'), 'utf8'));
  assert.equal(code, 1);
  assert.equal(receipt.steps[0].test_count, 0);
  assert.equal(receipt.steps[0].argv[2], '--test-reporter=tap');
  assert.equal(receipt.steps[0].report, 'step-1.tap');
  assert.equal(stopped, true);
});

test('retains a genuine JSON report and preserves a failed test receipt', async () => {
  const { root, results, path } = await fixture(
    'vitest-fixture',
    "#!/usr/bin/env node\nimport { writeFileSync } from 'node:fs';\nconst out=process.argv.find(x=>x.startsWith('--outputFile=')).slice(13); writeFileSync(out, JSON.stringify({numPassedTests:1,numFailedTests:0}));\n",
  );
  await chmod(path, 0o755);
  const success = await runValidation({
    root,
    directory: results,
    request,
    planner: () => ({ commands: [[path]], env: {} }),
    initializeGit: async () => {},
    fingerprintSource: stable,
    collect: noop,
  });
  assert.equal(success, 0);
  assert.deepEqual(JSON.parse(await readFile(join(results, 'step-1.vitest.json'), 'utf8')), {
    numPassedTests: 1,
    numFailedTests: 0,
  });
  const failed = await runValidation({
    root,
    directory: join(root, 'failed-results'),
    request,
    planner: () => ({ commands: [['node', '--test', join(root, 'missing.test.mjs')]], env: {} }),
    initializeGit: async () => {},
    fingerprintSource: stable,
    collect: noop,
  });
  const receipt = JSON.parse(await readFile(join(root, 'failed-results', 'validation.json'), 'utf8'));
  assert.equal(failed, 1);
  assert.equal(receipt.steps[0].exit_code, 1);
  assert.equal(receipt.steps[0].report, 'step-1.tap');
});

test('full suite replaces only the unbounded borrower Jest task', () => {
  const original = [
    ['pnpm', 'exec', 'turbo', 'run', 'test', '--filter=!@eichler/agent', '--concurrency=2'],
  ];
  const { commands, adapted } = fullCommands(original);
  assert.deepEqual(commands[0], [
    'pnpm',
    'exec',
    'turbo',
    'run',
    'test',
    '--filter=!@eichler/agent',
    '--filter=!@eichler/borrower',
    '--concurrency=2',
  ]);
  assert.deepEqual(commands.at(-1), [
    'pnpm',
    '--filter',
    '@eichler/borrower',
    'exec',
    'jest',
    '--maxWorkers=1',
  ]);
  assert.ok(commands.some((argv) => argv.includes('--filter=@eichler/borrower^...')));
  assert.equal(adapted.length, 1);
});
