import { copyFile, lstat, mkdir, readdir } from 'node:fs/promises';
import { dirname, join, relative } from 'node:path';

const directories = {
  'agent-web': ['apps/agent/test-results', 'apps/agent/playwright-report'],
  'browser-integration': ['test-results/browser-integration'],
  'employee-browser': ['test-results/employee-browser'],
};

export async function collectArtifacts(source, results, suite) {
  const files = [];
  const omitted = [];
  const omitArchives = ['browser-integration', 'employee-browser'].includes(suite);
  async function copy(path) {
    const origin = join(source, path);
    const stat = await lstat(origin);
    if (stat.isSymbolicLink() || (!stat.isFile() && !stat.isDirectory()))
      throw new Error(`Unsupported validation artifact: ${path}`);
    if (stat.isDirectory()) {
      for (const name of (await readdir(origin)).sort()) await copy(join(path, name));
    } else if (omitArchives && path.endsWith('.zip')) {
      // The repository disables service-browser traces. Do not publish an archive
      // left by an interrupted sanitizer without a separate completion receipt.
      omitted.push(path);
    } else {
      const destination = join(results, 'artifacts', path);
      await mkdir(dirname(destination), { recursive: true });
      await copyFile(origin, destination);
      files.push(relative(results, destination));
    }
  }
  for (const path of directories[suite] || []) {
    // Reject symlink ancestors as well as leaf symlinks before walking outputs.
    let current = source;
    let absent = false;
    for (const segment of path.split('/')) {
      current = join(current, segment);
      try {
        if (!(await lstat(current)).isDirectory())
          throw new Error(`Validation artifact directory is not an ordinary directory: ${path}`);
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
        absent = true;
        break;
      }
    }
    if (!absent) await copy(path);
  }
  return { files, omitted };
}
