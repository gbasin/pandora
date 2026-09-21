/**
 * Execute Eichler's validation planner inside Pandora's frozen source snapshot.
 *
 * The snapshot intentionally has no .git directory.  This runner creates a
 * private, synthetic repository solely for commands whose tooling reads Git;
 * it contains no host history, remotes, or credentials.  Do not call
 * `pnpm validate` here: its local queue entrypoint fingerprints Git and would
 * acquire a second scheduler slot.  The planner's underlying argv is the
 * authoritative command contract.
 */
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join, relative } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const source = process.cwd();
const results = '/workspace/results';
const configText = process.env.PANDORA_VALIDATION_CONFIG;
let config;
try {
  config = JSON.parse(configText || '');
} catch {
  config = null;
}

function fail(message) {
  throw new Error(message);
}

function validConfig(value) {
  if (
    !value ||
    value.version !== 1 ||
    typeof value.suite !== 'string' ||
    !Array.isArray(value.args) ||
    value.args.some((arg) => typeof arg !== 'string') ||
    typeof value.attempt !== 'string' ||
    !/^[a-f\d]{32}$/.test(value.attempt) ||
    (value.workers !== undefined && (!Number.isInteger(value.workers) || value.workers < 1 || value.workers > 2))
  )
    fail('PANDORA_VALIDATION_CONFIG must contain version, suite, args, and attempt.');
  return value;
}

const output = (directory, name) => join(directory, name);
const relativeOutput = (directory, path) => relative(directory, path) || '.';

function invoke(argv, environment = {}, cwd = source) {
  const childEnvironment = { ...process.env, ...environment };
  // A Node test worker inherits this marker. It must not turn an adapter child
  // into a recursive test-runner invocation.
  delete childEnvironment.NODE_TEST_CONTEXT;
  return new Promise((resolve) => {
    const child = spawn(argv[0], argv.slice(1), {
      cwd,
      env: childEnvironment,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let text = '';
    for (const stream of [child.stdout, child.stderr])
      stream.on('data', (chunk) => {
        const value = chunk.toString();
        text += value;
        process.stdout.write(value);
      });
    child.once('error', (error) => resolve({ code: 70, text: `${text}${error.message}` }));
    child.once('close', (code) => resolve({ code: code ?? 70, text }));
  });
}

async function syntheticGit(root, command = invoke) {
  const init = await command(['git', 'init', '--quiet'], {}, root);
  if (init.code) fail('Could not initialize the private synthetic repository.');
  for (const argv of [
    ['git', 'config', 'user.email', 'pandora@invalid'],
    ['git', 'config', 'user.name', 'Pandora snapshot'],
    ['git', 'add', '--all'],
    ['git', 'commit', '--quiet', '--no-gpg-sign', '-m', 'Frozen Pandora snapshot'],
  ]) {
    const result = await command(argv, {}, root);
    if (result.code) fail(`Synthetic Git setup failed: ${argv.join(' ')}`);
  }
}

function testBearing(argv) {
  return (
    argv.includes('--test') ||
    argv.includes('vitest') ||
    argv.includes('jest') ||
    (argv.includes('turbo') && argv.includes('test')) ||
    argv.includes('playwright') ||
    argv.includes('test:postgres') ||
    argv.includes('test:browser') ||
    argv.some((arg) => /(?:browser-integration|employee-browser)\/(?:pandora-)?run\.mjs$/.test(arg))
  );
}

export function tapCount(text) {
  const plain = text.replaceAll(/\x1b\[[0-?]*[ -/]*[@-~]/g, '');
  return [...plain.matchAll(/(?:#|ℹ) pass (\d+)(?:\s|$)/g)].reduce(
    (sum, match) => sum + Number(match[1]),
    0,
  );
}

export function consoleCount(text) {
  return [...text.matchAll(/Tests:\s+(\d+) passed/g)].reduce(
    (sum, match) => sum + Number(match[1]),
    0,
  );
}

export function browserCount(text) {
  return [...text.matchAll(/(?:^|\s)(\d+) passed(?:\s|\()/gm)].reduce(
    (sum, match) => sum + Number(match[1]),
    0,
  );
}

export function postgresCount(text) {
  return text.includes('Postgres invariants passed.') ? 1 : 0;
}

async function jsonCount(path) {
  try {
    const report = JSON.parse(await readFile(path, 'utf8'));
    if (report.stats)
      return (report.stats.expected || 0) + (report.stats.unexpected || 0) + (report.stats.flaky || 0);
    return (report.numPassedTests || 0) + (report.numFailedTests || 0);
  } catch {
    return 0;
  }
}

export function withReporters(argv, index, directory = results) {
  const command = [...argv];
  if (command.some((arg) => /(?:^|\/)vitest(?:$|[-.])/.test(arg))) {
    const report = output(directory, `step-${index}.vitest.json`);
    command.push('--reporter=default', '--reporter=json', `--outputFile=${report}`);
    return { argv: command, report, kind: 'json' };
  }
  if (command.some((arg) => /(?:^|\/)jest(?:$|[-.])/.test(arg))) {
    const report = output(directory, `step-${index}.jest.json`);
    command.push('--json', `--outputFile=${report}`);
    return { argv: command, report, kind: 'json' };
  }
  if (command.includes('--test') && !command.some((arg) => arg.startsWith('--test-reporter=')))
    command.splice(command.indexOf('--test') + 1, 0, '--test-reporter=tap');
  return { argv: command, report: output(directory, `step-${index}.tap`), kind: 'text' };
}

export function fullCommands(planned) {
  const adapted = [];
  const commands = [];
  for (const argv of planned) {
    if (!(argv.includes('turbo') && argv.includes('test'))) {
      commands.push(argv);
      continue;
    }
    const withoutBorrower = argv.flatMap((arg) =>
      arg === '--filter=!@eichler/agent'
        ? ['--filter=!@eichler/agent', '--filter=!@eichler/borrower']
        : [arg],
    );
    // Turbo otherwise may replay a cached test task's prior TAP output. The
    // receipt must describe tests executed for this frozen attempt.
    commands.push([...withoutBorrower, '--force']);
    commands.push([
      'pnpm',
      'exec',
      'turbo',
      'run',
      'build',
      '--filter=@eichler/borrower^...',
      '--concurrency=1',
    ]);
    commands.push(['pnpm', '--filter', '@eichler/borrower', 'exec', 'jest', '--maxWorkers=1']);
    adapted.push({
      reason: 'The native Turbo package test invokes borrower Jest without the remote worker cap.',
      replaced: argv,
      added: commands.slice(-3),
      turbo_force: true,
    });
  }
  return { commands, adapted };
}

export async function runValidation({
  root = source,
  directory = results,
  request: suppliedRequest = config,
  planner,
  runner = invoke,
  initializeGit = syntheticGit,
  collect,
  service: suppliedService,
  fingerprintSource,
} = {}) {
  const request = validConfig(suppliedRequest);
  await mkdir(directory, { recursive: true });
  const { plan } = planner ? { plan: planner } : await import(join(root, 'tools/validation/plan.mjs'));
  // The VM main slot is 1000m CPU.  Keeping the planner at one worker gives a
  // fixed resource bound across Vitest, Node test, and Jest.
  // `postgres` normally delegates to heavy.mjs, which would create another
  // stack. Pandora owns the isolated services, so select the planner's CI
  // command shape for this one case.
  const plannerEnvironment = request.suite === 'postgres' ? { GITHUB_ACTIONS: 'true' } : {};
  const priorGithubActions = process.env.GITHUB_ACTIONS;
  Object.assign(process.env, plannerEnvironment);
  const planned = plan(root, request.suite, request.args, request.workers ?? 1);
  if (priorGithubActions === undefined) delete process.env.GITHUB_ACTIONS;
  else process.env.GITHUB_ACTIONS = priorGithubActions;
  let service = suppliedService;
  let browser;
  if (request.suite === 'browser-integration') {
    const { prepareBrowserRunner } = await import(join(root, 'pandora-validation-stack.mjs'));
    try {
      browser = await prepareBrowserRunner(root, request.attempt);
    } catch (error) {
      throw new Error(`Pandora does not support this browser runner revision. Keep the repository runner unchanged and ask the operator to update the adapter. ${error.message}`);
    }
  }
  await initializeGit(root, runner);
  const fingerprint =
    fingerprintSource || (await import(join(root, 'tools/validation/state.mjs'))).fingerprint;
  const before = fingerprint(root);
  if (request.suite === 'postgres' && !service) {
    const { startValidationStack } = await import(join(root, 'pandora-validation-stack.mjs'));
    service = await startValidationStack(root, new AbortController().signal);
  }
  let commands = planned.commands;
  let adaptations = [];
  if (request.suite === 'full') ({ commands, adapted: adaptations } = fullCommands(commands));
  const steps = [];
  let exitCode = 0;
  let detail = '';
  try {
    for (const [index, original] of commands.entries()) {
      const argv =
        browser && original.includes('tools/browser-integration/run.mjs')
          ? original.map((arg) => (arg === 'tools/browser-integration/run.mjs' ? browser.path : arg))
          : original;
      const step = withReporters(argv, index + 1, directory);
      const result = await runner(step.argv, { ...planned.env, ...service?.env }, root);
      let count = null;
      if (testBearing(step.argv)) {
        if (step.kind === 'json') count = await jsonCount(step.report);
        else count = tapCount(result.text) || consoleCount(result.text) || browserCount(result.text);
        if (step.argv.includes('test:postgres')) count ||= postgresCount(result.text);
        if (step.kind === 'text') await writeFile(step.report, result.text);
      }
      const receipt = {
        argv: step.argv,
        exit_code: result.code,
        test_count: count,
        report:
          testBearing(step.argv) && (step.kind === 'text' || existsSync(step.report))
            ? relativeOutput(directory, step.report)
            : null,
      };
      steps.push(receipt);
      if (result.code) {
        exitCode = result.code;
        detail = `Step ${index + 1} exited ${result.code}.`;
        break;
      }
      if (count !== null && count < 1) {
        exitCode = 1;
        detail = `Step ${index + 1} did not report an authentic executed test.`;
        break;
      }
    }
  } finally {
    try {
      await service?.stack.stop();
    } catch (error) {
      exitCode ||= 70;
      detail ||= `Pandora service stack cleanup failed: ${error.message}`;
    }
  }
  if (exitCode === 0 && fingerprint(root) !== before) {
    exitCode = 1;
    detail = 'Source changed during validation. Results are stale.';
  }
  let artifacts = { files: [], omitted: [] };
  try {
    const collectArtifacts = collect || (await import(join(root, 'pandora-validation-artifacts.mjs'))).collectArtifacts;
    artifacts = await collectArtifacts(root, directory, request.suite, request.attempt);
  } catch (error) {
    exitCode ||= 70;
    detail ||= `Validation artifact collection failed: ${error.message}`;
  }
  const report = {
    version: 1,
    attempt: request.attempt,
    suite: request.suite,
    args: request.args,
    exit_code: exitCode,
    synthetic_git: true,
    steps,
    artifacts,
    ...(adaptations.length || browser
      ? {
          adaptations: [
            ...adaptations,
            ...(browser
              ? [
                  {
                    reason: browser.adaptation,
                    source_sha256: browser.source_sha256,
                    transformed_sha256: browser.transformed_sha256,
                    runner: browser.path,
                  },
                ]
              : []),
          ],
        }
      : {}),
    ...(detail ? { detail } : {}),
  };
  await writeFile(output(directory, 'validation.json'), JSON.stringify(report, null, 2) + '\n');
  await writeFile(output(directory, 'exit-code'), `${exitCode}\n`);
  return exitCode;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  let code = 70;
  try {
    code = await runValidation();
  } catch (error) {
    await mkdir(results, { recursive: true });
    const request = config && typeof config === 'object' ? config : {};
    await writeFile(
      output(results, 'validation.json'),
      JSON.stringify({ version: 1, attempt: request.attempt, suite: request.suite, args: request.args, exit_code: 70, steps: [], detail: error.message }, null, 2) + '\n',
    );
    await writeFile(output(results, 'exit-code'), '70\n');
    console.error(error.message);
  }
  process.exitCode = code;
}
