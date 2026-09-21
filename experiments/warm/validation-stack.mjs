import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const digest = (contents) => createHash('sha256').update(contents).digest('hex');
const BROWSER_OPTIONS = ['signal', 'deployment', 'env', 'output', 'log'];

/**
 * Start Eichler's validation stack against Pandora-owned loopback services.
 * The caller owns stack.stop() and applies env to the validation child.
 */
export async function startValidationStack(root, signal) {
  const source = resolve(root);
  const { startInstance } = await import(
    pathToFileURL(join(source, 'tools/stack/instance.mjs')).href,
  );
  const stack = await startInstance({
    external: true,
    signal,
    deployment: 'ci',
    env: { NODE_ENV: 'test' },
    output: () => {},
    log: () => {},
  });
  return {
    stack,
    env: {
      DATABASE_OWNER_URL: stack.databaseOwnerUrl,
      DATABASE_WS_PROXY: stack.databaseProxy,
      IKE_API_URL: stack.api,
      JOURNEY_CONCURRENCY: '4',
    },
  };
}

/**
 * Create an attempt-private browser runner that uses Pandora's external
 * services. This is deliberately a bounded textual seam, not a JavaScript
 * parser: a source change to the inspected call shape requires a new adapter.
 * Keeping it beside run.mjs preserves that runner's relative paths.
 */
export async function prepareBrowserRunner(root) {
  const sourceRoot = resolve(root);
  const directory = join(sourceRoot, 'tools/browser-integration');
  const sourcePath = join(directory, 'run.mjs');
  const path = join(directory, 'pandora-run.mjs');
  const source = await readFile(sourcePath, 'utf8');
  const marker = 'stack = await startInstance({';
  const matches = source.split(marker).length - 1;
  if (matches !== 1)
    throw new Error(
      `Browser runner adaptation requires exactly one ${JSON.stringify(marker)}; found ${matches}.`,
    );
  if (/\bexternal\s*:/.test(source))
    throw new Error('Browser runner adaptation refuses a source that already sets external.');
  const start = source.indexOf(marker);
  const end = source.indexOf('\n  });', start);
  if (end < 0)
    throw new Error('Browser runner adaptation requires the inspected startInstance call terminator.');
  const call = source.slice(start, end + '\n  });'.length);
  if (/\.\.\./.test(call))
    throw new Error('Browser runner adaptation refuses option spreads in startInstance.');
  const keys = [...call.matchAll(/^ {4}([A-Za-z_$][\w$]*)(?::|,)/gm)].map((match) => match[1]);
  if (keys.length !== BROWSER_OPTIONS.length || keys.some((key, index) => key !== BROWSER_OPTIONS[index]))
    throw new Error(
      `Browser runner adaptation requires startInstance options ${BROWSER_OPTIONS.join(', ')}.`,
    );
  const transformed = source.replace(marker, `${marker}\n    external: true,`);
  await mkdir(directory, { recursive: true });
  await writeFile(path, transformed, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
  return {
    path,
    source_sha256: digest(source),
    transformed_sha256: digest(transformed),
    adaptation: 'startInstance external services',
  };
}
