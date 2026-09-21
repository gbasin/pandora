import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm, symlink } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import test from 'node:test';
import { collectArtifacts } from './validation-artifacts.mjs';

test('returns declared browser evidence and omits service archives', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'pandora-artifacts-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const source = join(root, 'source');
  const results = join(root, 'results');
  const path = 'test-results/browser-integration';
  await mkdir(join(source, path), { recursive: true });
  await writeFile(join(source, path, 'stack.log'), 'sanitized');
  await writeFile(join(source, path, 'trace.zip'), 'unverified');
  const receipt = await collectArtifacts(source, results, 'browser-integration');
  assert.deepEqual(receipt.files, [`artifacts/${path}/stack.log`]);
  assert.deepEqual(receipt.omitted, [`${path}/trace.zip`]);
  assert.equal(await readFile(join(results, receipt.files[0]), 'utf8'), 'sanitized');
  assert.deepEqual(await collectArtifacts(source, results, 'unit'), { files: [], omitted: [] });
});

test('rejects symlink ancestors and leaf artifacts', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'pandora-artifacts-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const source = join(root, 'source');
  const results = join(root, 'results');
  await mkdir(source);
  await mkdir(join(root, 'external', 'browser-integration'), { recursive: true });
  await symlink(join(root, 'external'), join(source, 'test-results'));
  await assert.rejects(collectArtifacts(source, results, 'browser-integration'), /ordinary directory/);
  await rm(join(source, 'test-results'));
  await mkdir(join(source, 'test-results', 'browser-integration'), { recursive: true });
  await symlink(join(root, 'external'), join(source, 'test-results/browser-integration/link'));
  await assert.rejects(collectArtifacts(source, results, 'browser-integration'), /Unsupported/);
});
