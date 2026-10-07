"""Offline diagnosis of a client run, using only its saved records."""
import json
import re
from pathlib import Path

from ..engine import retry
from . import hints
from .doctor import FAIL, INFO, OK, WARN, check

# These are implications of a recorded cause, not new observations of a worker.
AREAS = {
    'prepare-failed': ('prepare', 'golden install_command and toolchain'),
    'prepare-command-failed': ('prepare_command', 'job prepare_command and transferred source'),
    'prepare-command-execution-failed': ('prepare_command', 'worker preparation supervisor'),
    'prepare-command-oom': ('prepare_command', 'job size class and preparation memory'),
    'prepare-command-timeout': ('prepare_command', 'job timeout and preparation duration'),
    'clone-failed': ('clone', 'worker golden, storage pool, and instance cloning'),
    'disk-quota': ('clone', 'worker instance disk quota'),
    'execution-failed': ('execute', 'worker command launch and input'),
    'instance-lost': (None, 'worker instance lifecycle'),
    'supervisor-gone': (None, 'worker engine supervision'),
    'destroy-incomplete': ('destroy', 'worker instance, volume, and network cleanup'),
    'partition-unverified': ('verify', 'shard inventory and reported test identities'),
    'shard-failed': (None, 'shard results and their recorded causes'),
    'admission-timeout': ('admission', 'worker capacity and admission wait'),
    'queue-timeout': ('queue', 'worker queue and wait bound'),
    'disk-floor': ('admission', 'worker free disk floor'),
    'admission-refused': ('admission', 'worker memory budget and occupied slots'),
    'worker-lost': (None, 'worker connection; execution may still be active'),
}


def read_record(path, checks):
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError('expected a JSON object')
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        checks.append(check(path.name, FAIL, 'cannot read saved record: %s' % error))
        return None


def from_run(state, run_id):
    """A doctor report with historical context; no subprocess or daemon calls."""
    state = Path(state).expanduser()
    checks = []
    report = {'ok': False, 'mode': 'recorded', 'state': str(state),
              'run': {'id': run_id}, 'checks': checks}
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', run_id):
        checks.append(check('run id', FAIL, 'use the run id shown by pandora ps'))
        return report
    directory = state / 'runs' / run_id
    # Refuse a run-directory symlink as well as traversal in the supplied id.
    if directory.is_symlink():
        checks.append(check('run record', FAIL, 'the run directory is a symlink'))
        return report
    meta = read_record(directory / 'meta.json', checks)
    result = read_record(directory / 'result.json', checks)
    if meta is None and result is None:
        checks.append(check('run record', FAIL, 'no readable meta.json or result.json for %s'
                            % run_id))
        return report
    meta, result = meta or {}, result or {}
    evidence = result.get('evidence')
    evidence = evidence if isinstance(evidence, dict) else {}
    refusal = meta.get('refusal')
    refusal = refusal if isinstance(refusal, dict) else {}
    outcome = result.get('outcome') or meta.get('state')
    # Preserve explicitly recorded future causes rather than naming them engine-error.
    cause = evidence.get('cause') if result else refusal.get('cause')
    cause = cause if isinstance(cause, str) else None
    if not cause and result.get('outcome') == 'infra_failed':
        try:
            cause = retry.cause_of(dict(result, evidence=evidence))
        except (AttributeError, TypeError, ValueError) as error:
            checks.append(check('saved cause', FAIL, 'invalid cause evidence: %s' % error))
    phase, area = AREAS.get(cause, (None, None))
    retryable, retry_reason = retry.retryable(cause) if cause else (None, None)
    if cause and result and result.get('outcome') != 'infra_failed':
        retryable, retry_reason = False, 'the cause retry table applies only to infra_failed outcomes'
    last_reported = meta.get('phase')
    recorded_phase = last_reported if last_reported not in (
        'finished', 'passed', 'infra_failed', 'command_failed', 'oom', 'timed_out', 'cancelled') else None
    phase_source = 'recorded' if recorded_phase else ('cause' if phase else None)
    phase = recorded_phase or phase
    if not phase and isinstance(evidence.get('preparation'), dict):
        phase, phase_source = 'prepare_command', 'evidence'
    tail = hints.log_tail(directory / 'log')
    hint = result.get('hint') or meta.get('hint')
    if not hint and result:
        # No exists()/git-ignore probes: historical diagnosis uses recorded facts only.
        try:
            # Executable hints name the worker. Never derive them from a local log.
            remote_tail = tail if meta.get('lane') == 'remote' else ''
            named = hints.for_run_named(dict(result, evidence=evidence),
                                       worktree=None, tail=remote_tail)
            hint = named[1] if named else None
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            checks.append(check('saved hint', WARN, 'cannot derive a hint: %s' % error))
    run = report['run']
    run.update(repo=meta.get('repo') or result.get('repo'),
               job=meta.get('job') or result.get('job'), worktree=meta.get('worktree'),
               argv=meta.get('argv') or result.get('argv'), lane=meta.get('lane'),
               worker=meta.get('worker') or result.get('worker'),
               remote=meta.get('remote') or result.get('run_id'),
               outcome=outcome, exit_code=(meta.get('exit_code')
                                         if meta.get('exit_code') is not None
                                         else result.get('cli_exit', result.get('observed_exit'))),
               phase=phase, phase_source=phase_source, last_reported_phase=last_reported, cause=cause,
               **{'class': cause or outcome, 'hint': hint},
               retryable=retryable, retry_reason=retry_reason,
               implicated=area, evidence=evidence, log_tail=tail,
               pre_accept=meta.get('pre_accept') or {},
               freeze_steps=meta.get('freeze_steps') or {},
               transfer=meta.get('transfer') or {})
    context = 'repo=%s job=%s lane=%s remote=%s' % tuple(
        run.get(key) or 'not recorded' for key in ('repo', 'job', 'lane', 'remote'))
    checks.append(check('recorded context', INFO, context, worktree=run['worktree'],
                        worker=run['worker']))
    failed = (outcome in ('infra_failed', 'command_failed', 'oom', 'timed_out',
                         'stale', 'refused', 'incomplete')
              or (outcome not in ('queued', 'running', 'cancelled')
                  and run['exit_code'] is not None and run['exit_code'] != 0))
    status = FAIL if failed else (OK if outcome == 'passed' else INFO)
    checks.append(check('recorded outcome', status,
                        'class=%s outcome=%s phase=%s%s' % (
                            run['class'] or 'unknown', outcome or 'unknown', phase or 'not recorded',
                            ' (inferred from %s)' % phase_source
                            if phase_source in ('cause', 'evidence') else ''),
                        **{'class': run['class'], 'cause': cause, 'phase': phase}))
    if cause:
        checks.append(check('cause retry policy', INFO,
                            'retryable=%s: %s. This is the cause policy; it does not authorize replay.'
                            % ('yes' if run['retryable'] else 'no', run['retry_reason']),
                            retryable=run['retryable'], reason=run['retry_reason']))
    if area:
        checks.append(check('implicated checks', INFO, area))
    if hint:
        checks.append(check('recorded hint', INFO, 'hint=%s' % hint, hint=hint))
    if not result:
        checks.append(check('saved result', WARN,
                            'no result.json; diagnosis uses client metadata, not a worker verdict'))
    report['ok'] = not any(item['status'] == FAIL for item in checks)
    return report
