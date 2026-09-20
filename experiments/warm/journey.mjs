import { mkdir, writeFile } from 'node:fs/promises';
import { startInstance } from '/workspace/source/tools/stack/instance.mjs';
import { loadJourneys } from '/workspace/source/packages/scenarios/src/journeys/index.ts';
import { runJourney } from '/workspace/source/packages/scenarios/src/runner.ts';
import { checkRoutes } from '/workspace/source/packages/scenarios/src/cli/plan.ts';

const controller = new AbortController();
process.on('SIGTERM', () => controller.abort());
process.on('SIGINT', () => controller.abort());
let stack;
let result;
let status = 1;
const redact = (value) => {
  let text = String(value);
  for (const secret of stack?.secrets ?? []) text = text.split(secret).join('[redacted]');
  return text;
};
try {
  console.log('[pandora] applying migrations and starting the local Workers runtime');
  // Startup output contains generated credentials. Do not stream it into agent logs.
  stack = await startInstance({ external: true, signal: controller.signal, output: () => {} });
  console.log('[pandora] Workers runtime ready; running journey S0-01');
  const journey = (await loadJourneys()).find((j) => j.id === 'S0-01');
  if (!journey) throw new Error('Journey S0-01 is absent from this snapshot');
  result = await runJourney(journey, { baseUrl: stack.api, printPrincipals: false });
  if (result.status === 'pass' && result.writeRoutes) await checkRoutes(result.id, result.writeRoutes, false);
  if (stack.crashed) throw new Error(`Workers runtime exited unexpectedly: ${stack.crashed}`);
  status = result.status === 'pass' ? 0 : 1;
} catch (error) {
  result = { id: 'S0-01', status: 'fail', detail: redact(error.stack ?? error.message) };
} finally {
  if (stack) {
    try { await stack.stop(); }
    catch (error) { status = 1; result = { id: 'S0-01', status: 'fail', detail: redact(error.message) }; }
  }
  const report = { journey: result?.id ?? 'S0-01', status: result?.status ?? 'fail', detail: redact(result?.detail ?? '') };
  await mkdir('/workspace/results', { recursive: true });
  await writeFile('/workspace/results/journey.json', JSON.stringify(report, null, 2) + '\n');
  await writeFile('/workspace/results/exit-code', String(status) + '\n');
  console.log(JSON.stringify(report));
  process.exitCode = status;
}
