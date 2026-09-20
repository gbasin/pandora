import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
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
let config;
let ledgerExpected = true;
const redact = (value) => {
  let text = String(value);
  for (const secret of stack?.secrets ?? []) text = text.split(secret).join('[redacted]');
  return text;
};
const loadConfig = () => {
  const value = process.env.PANDORA_JOURNEY_CONFIG;
  if (!value) throw new Error('Pandora journey configuration is required');
  const parsed = JSON.parse(value);
  if (
    !/^(?:S[0-6]|SX)-\d{2}$(?![\s\S])/.test(parsed?.id) ||
    typeof parsed.update !== 'boolean' ||
    (parsed.fault !== null && parsed.fault !== 'dropped')
  ) {
    throw new Error('Unsupported Pandora journey configuration');
  }
  return parsed;
};
const captureProposals = async () => {
  if (!config?.update) return [];
  const proposalPaths = [
    ...(ledgerExpected ? [`packages/scenarios/fixtures/${config.id}.ledger.jsonl`] : []),
    'packages/scenarios/fixtures/write-routes.json',
  ];
  const proposals = [];
  for (const path of proposalPaths) {
    const source = `/workspace/source/${path}`;
    const destination = `/workspace/results/updates/${path}`;
    await mkdir(destination.slice(0, destination.lastIndexOf('/')), { recursive: true });
    await copyFile(source, destination);
    const contents = await readFile(destination);
    proposals.push({ path: `updates/${path}`, sha256: createHash('sha256').update(contents).digest('hex') });
  }
  return proposals;
};
let proposals = [];
try {
  config = loadConfig();
  const journey = (await loadJourneys()).find((candidate) => candidate.id === config.id);
  if (!journey) throw new Error(`Journey ${config.id} is absent from this snapshot`);
  ledgerExpected = journey.surfaces.includes('api');
  console.log('[pandora] applying migrations and starting the local Workers runtime');
  // Startup output contains generated credentials. Do not stream it into agent logs.
  stack = await startInstance({ external: true, signal: controller.signal, output: () => {} });
  console.log(`[pandora] Workers runtime ready; running journey ${config.id}`);
  result = await runJourney(journey, {
    baseUrl: stack.api,
    printPrincipals: false,
    update: config.update,
    ...(config.fault ? { fault: config.fault } : {}),
  });
  if (result.status === 'pass' && result.writeRoutes) await checkRoutes(result.id, result.writeRoutes, config.update);
  if (stack.crashed) throw new Error(`Workers runtime exited unexpectedly: ${stack.crashed}`);
  status = result.status === 'pass' ? 0 : 1;
} catch (error) {
  result = { id: config?.id ?? 'unknown', status: 'fail', detail: redact(error.stack ?? error.message) };
} finally {
  if (stack) {
    try { await stack.stop(); }
    catch (error) {
      status = 1;
      result = { id: config?.id ?? 'unknown', status: 'fail', detail: redact(error.message) };
    }
  }
  await mkdir('/workspace/results', { recursive: true });
  try {
    proposals = await captureProposals();
  } catch (error) {
    status = 1;
    result = {
      id: config?.id ?? 'unknown',
      status: 'fail',
      detail: redact(`Unable to capture update proposals: ${error.message}`),
    };
  }
  const report = {
    journey: result?.id ?? config?.id ?? 'unknown',
    status: status === 0 && result?.status === 'pass' ? 'pass' : 'fail',
    detail: redact(result?.detail ?? ''),
    update: config?.update ?? false,
    fault: config?.fault ?? null,
    ledger_expected: ledgerExpected,
    proposals,
  };
  await writeFile('/workspace/results/journey.json', JSON.stringify(report, null, 2) + '\n');
  await writeFile('/workspace/results/exit-code', String(status) + '\n');
  console.log(JSON.stringify(report));
  process.exitCode = status;
}
