import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { prepareBrowserRunner, startValidationStack } from './validation-stack.mjs';

const hash = (value) => createHash('sha256').update(value).digest('hex');
const runner = `let stack;\nstack = await startInstance({\n    signal,\n    deployment: 'ci',\n    env: { NODE_ENV: 'test' },\n    output: (chunk) => { workerLog += chunk; },\n    log: () => {},\n  });\n`;

async function fixture(contents = runner) {
  const root = await mkdtemp(join(tmpdir(), 'pandora-validation-stack-'));
  const directory = join(root, 'tools/browser-integration');
  await mkdir(directory, { recursive: true });
  const source = join(directory, 'run.mjs');
  await writeFile(source, contents);
  return { root, source, output: join(directory, 'pandora-run.mjs') };
}

test('prepareBrowserRunner transforms precisely once and leaves the source intact', async () => {
  const { root, source, output } = await fixture();
  const before = await readFile(source, 'utf8');

  const metadata = await prepareBrowserRunner(root);

  const transformed = await readFile(output, 'utf8');
  assert.equal(await readFile(source, 'utf8'), before);
  assert.equal(
    transformed,
    before.replace('stack = await startInstance({', 'stack = await startInstance({\n    external: true,'),
  );
  assert.deepEqual(metadata, {
    path: output,
    source_sha256: hash(before),
    transformed_sha256: hash(transformed),
    adaptation: 'startInstance external services',
  });
});

test('prepareBrowserRunner rejects zero or multiple adaptation seams', async () => {
  for (const source of ['await startInstance({});', `${runner}\n${runner}`]) {
    const { root, source: sourcePath, output } = await fixture(source);
    await assert.rejects(prepareBrowserRunner(root), /exactly one/);
    assert.equal(await readFile(sourcePath, 'utf8'), source);
    await assert.rejects(readFile(output), { code: 'ENOENT' });
  }
});

test('prepareBrowserRunner rejects external options, spreads, and a changed option shape', async () => {
  const cases = [
    [
      runner.replace('    signal,', '    external: false,\n    signal,'),
      /already sets external/,
    ],
    [runner.replace('    signal,', '    ...options,\n    signal,'), /option spreads/],
    [
      runner.replace('    signal,', '    apiPort: 0,\n    signal,'),
      /requires startInstance options/,
    ],
  ];
  for (const [source, error] of cases) {
    const { root, source: sourcePath, output } = await fixture(source);
    await assert.rejects(prepareBrowserRunner(root), error);
    assert.equal(await readFile(sourcePath, 'utf8'), source);
    await assert.rejects(readFile(output), { code: 'ENOENT' });
  }
});

test('startValidationStack dynamically loads the target stack and returns heavy-runner environment', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pandora-validation-stack-instance-'));
  const directory = join(root, 'tools/stack');
  await mkdir(directory, { recursive: true });
  await writeFile(
    join(directory, 'instance.mjs'),
    "export async function startInstance(options) { globalThis.validationStackOptions = options; return { databaseOwnerUrl: 'owner-url', databaseProxy: 'proxy-url', api: 'api-url' }; }",
  );

  const signal = new AbortController().signal;
  const result = await startValidationStack(root, signal);

  assert.equal(globalThis.validationStackOptions.external, true);
  assert.equal(globalThis.validationStackOptions.signal, signal);
  assert.equal(globalThis.validationStackOptions.deployment, 'ci');
  assert.deepEqual(globalThis.validationStackOptions.env, { NODE_ENV: 'test' });
  assert.equal(typeof globalThis.validationStackOptions.output, 'function');
  assert.equal(typeof globalThis.validationStackOptions.log, 'function');
  assert.equal(globalThis.validationStackOptions.output('generated credential'), undefined);
  assert.equal(globalThis.validationStackOptions.log('stack ready'), undefined);
  assert.deepEqual(result.env, {
    DATABASE_OWNER_URL: 'owner-url',
    DATABASE_WS_PROXY: 'proxy-url',
    IKE_API_URL: 'api-url',
    JOURNEY_CONCURRENCY: '4',
  });
});
