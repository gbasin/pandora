"""Where one claimed command runs: the job's `where`, or the caller's override.

`PANDORA_OFF=1` used to be the only lever a caller had over placement, and it is
a blunt one: the command runs here with no queue, no admission, no receipt and
no row in `pandora stats`. The override keeps all of that. `--local` puts a
remote job into the local lane with its own size class, behind the same budget,
pause gate and one-run-per-worktree rule as any local job; `--remote` sends a
local job to the worker through the same freeze, ship and submit.

An override is a request, not a guess. When the job cannot honestly run where
it is asked to -- its run step leans on something only the other side has --
the answer is a refusal with exit 64 that says why, never a quiet run somewhere
else. And an explicit `--remote` that the worker cannot take is an error, not a
fallback: the fallback lane exists for commands whose caller did not say where,
and this caller did.
"""
from ..errors import Refused
from ..exits import USAGE

WHERE = ('local', 'remote')
ENV = 'PANDORA_WHERE'


def parse(value, *, source=ENV):
    """None for "not asked", else `local` or `remote`. Anything else is a ValueError.

    Empty is "not asked" so that `PANDORA_WHERE= pnpm check` is the same as not
    setting it, which is how every other Pandora variable reads.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text not in WHERE:
        raise ValueError('%s=%s is not a placement; use local or remote (or unset it)'
                         % (source, text))
    return text


def refuse(message):
    error = Refused(message)
    error.code, error.exit = 'placement', USAGE
    return error


def writes_back(plan):
    """True when this invocation carries write-back intent: the plan has armed
    write-back outputs, or the job's update option was typed. The one question
    asked wherever "is this a write-back run" is decided."""
    plan = plan or {}
    return bool(plan.get('writeback') or (plan.get('options') or {}).get('update'))


def why_not_local(job, plan=None):
    """The reason a job's run step cannot run on this Mac, or None.

    A sharded job is a fan-out across worker instances. With a `plan` step, the
    run argv is written against what that step built on the worker (acme's
    surface runner is `run ... --no-build`), so running it here would test
    nothing that was built; without one, "the whole suite in one process on this
    Mac" is the 2026-09-22 accident with a flag on it. Either way the loader
    already refuses `shards` with `where = "local"`, and an override must not be
    a way around a rule the configuration itself cannot state.

    A run that writes back is refused for the same shape of reason: the
    proposal, the staleness check and the conflict report are the engine's, and
    a local run has none of them -- it would write the files in place with no
    check at all, which is write-back in name only. `plan` carries the intent;
    callers without one get only the job-level checks.
    """
    if job.get('shards'):
        return ('jobs.%s is sharded across worker instances%s, so it cannot run in the '
                'local lane' % (job['id'], ' and its run step uses what the worker\'s plan '
                                'step built' if job['shards'].get('plan') else ''))
    if writes_back(plan):
        return ('jobs.%s is asked to write files back, and write-back happens only on the '
                'worker, so it cannot run in the local lane' % job['id'])
    return None


def why_not_remote(job):
    """The reason a local job cannot be shipped to the worker, or None.

    `singleton` is a machine-wide rule for something that holds this Mac's ports
    (the dev stack); on the worker it would hold nothing anyone can reach.
    `evidence` outputs are paths a local run leaves in the worktree for the
    receipt; a remote run writes them on the worker, and nothing brings evidence
    home -- only artifacts -- so the receipt would record them as missing.
    """
    if job.get('singleton'):
        return ('jobs.%s is a singleton that holds resources on this machine, so it '
                'cannot run on the worker' % job['id'])
    evidence = [path for output in job.get('outputs') or []
                if output.get('kind') == 'evidence' for path in output['paths']]
    if evidence:
        return ('jobs.%s declares evidence outputs (%s) that a worker run would not bring '
                'home, so it cannot run on the worker' % (job['id'], ', '.join(evidence[:3])))
    return None


def decide(job, plan, override):
    """Returns {'where', 'plan', 'record', 'reason'}; raises the 64 refusal.

    `record` is what the run's `meta.json` and `result.json` carry: where it
    ran, where the job says it runs, what the caller asked for, and whether the
    ask changed anything. `reason` is the run's lane reason -- `override:<where>`
    when the ask changed the lane, empty otherwise -- in the same field a
    fallback writes `fallback:<cause>` into, because both answer "why is this
    run in this lane".
    """
    declared = job['where']
    where = override or declared
    changed = where != declared
    if changed and where == 'local':
        problem = why_not_local(job, plan)
        if problem:
            advice = (' Retry it without the override.'
                      if writes_back(plan) else
                      ' As a last resort, PANDORA_OFF=1 runs it here with no Pandora at all, '
                      'outside the queue.')
            raise refuse('%s. Drop --local/PANDORA_WHERE.%s' % (problem, advice))
        plan = dict(plan, where='local')
    elif changed and where == 'remote':
        problem = why_not_remote(job)
        if problem:
            raise refuse('%s. Drop --remote/PANDORA_WHERE.' % problem)
        # A local job runs in a real checkout, and anything it does with git --
        # acme's planner fingerprints the tree with `git rev-parse` and `git
        # diff` -- works there. A worker run arrives without `.git`, and the
        # loader forbids a local job from saying it needs one. So the override
        # asks for the synthetic repository rather than hoping the job never
        # touches git: ~3 s against a run that fails for a reason nobody typed.
        plan = dict(plan, where='remote', git='synthetic')
    record = {'where': where, 'declared': declared, 'override': override,
              'overridden': changed}
    return {'where': where, 'plan': plan, 'record': record,
            'reason': 'override:' + where if changed else ''}
