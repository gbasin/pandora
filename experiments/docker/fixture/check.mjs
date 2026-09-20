import { mkdir, readFile, writeFile } from 'node:fs/promises';
const value = (await readFile('value.txt', 'utf8')).trim();
await mkdir('dist', { recursive: true });
await writeFile('dist/result.json', JSON.stringify({ value }) + '\n');
if (value.startsWith('broken-')) {
  console.error(`Expected a repaired value, received ${value}`);
  process.exitCode = 1;
} else {
  console.log(`Validated ${value}`);
}
