"""Hints: the one sentence a reader of a failure would have wanted first.

A result already says what happened. A hint says what to *do*, and only when the
evidence already on hand names the action. There are six rules, and every one of
them is a function of facts that were measured -- a peak against a ceiling, a
wall clock against a limit, a declared report that is not there, two manifests
that differ, two verdicts on one input that disagree, a path the command named
that exists here and was not shipped.

What this file deliberately is not: a guesser. No model, no pattern library, no
"this looks like a flaky test" -- the flaky rule fires only on two recorded
attempts with the same input and command that reached opposite verdicts. A rule that cannot point at the measurement it
used does not belong here, because a wrong hint is worse than none -- an agent
acts on it, and then the next twenty minutes are spent on the wrong thing.

The rules are pure functions of one `facts` dictionary so that both sides can
run them. The engine has the outcome, the peak, the ceiling and the collected
outputs, so it attaches a hint at collect time. The client has the worktree, the
snapshot manifest and the log, so it fills in the two rules the engine cannot
see. `hint_for` returns the first rule that fires, and order is worst-first: a
run that was killed has nothing to say about a missing report.
"""
import re

# How much of the log the path rule is allowed to look at, and how many distinct
# path-like tokens it will consider. Both are caps rather than tunings: the rule
# is a cheap scan over a failure's tail, and a 200 MiB log must not turn it into
# a search.
LOG_TAIL_BYTES = 65536
MAX_TOKENS = 200
# A token that could be a path: at least one separator or a leading `./`, no
# whitespace, no shell metacharacters that would make it a fragment of a command.
PATH_TOKEN = re.compile(r'(?<![\w/.-])((?:\./|\.\./)?[\w.@+-]+(?:/[\w.@+-]+)+/?)')


def path_tokens(text, cap=MAX_TOKENS):
    """Distinct path-like tokens in `text`, in the order they appear.

    Punctuation that commonly wraps a path in an error message -- quotes, the
    trailing comma of a stack frame, a closing parenthesis -- is stripped,
    because `Cannot find module 'tmp/fixture.json'` is the shape this rule is
    written for.
    """
    seen, out = set(), []
    for match in PATH_TOKEN.finditer(text or ''):
        token = match.group(1).strip('\'"`,;:)]}').rstrip('/')
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= cap:
            break
    return out


def mib(value):
    return int(value or 0)


# -- the rules ---------------------------------------------------------------

def oom(facts):
    """Killed for memory. Two different cures, and the evidence says which."""
    if facts.get('outcome') != 'oom':
        return None
    evidence = facts.get('evidence') or {}
    job = facts.get('job') or 'this job'
    if (evidence.get('reason') or '') == 'memory-thrash':
        return ('watchdog killed for file-cache thrash; likely a large build or install '
                '-- declare size large for job %s (peak %d MiB of a %d MiB ceiling)'
                % (job, mib(facts.get('peak_mib')), mib(facts.get('ceiling_mib'))))
    return ('raise the size class for job %s (peak %d MiB of a %d MiB ceiling)'
            % (job, mib(facts.get('peak_mib')), mib(facts.get('ceiling_mib'))))


def timed_out(facts):
    """Killed by the wall clock. The only cure Pandora can name is the wall."""
    if facts.get('outcome') != 'timed_out':
        return None
    seconds = float(facts.get('wall_seconds') or 0)
    return ('timed out after %.0fs; raise timeout_minutes for job %s, or split it into '
            'shards' % (seconds, facts.get('job') or 'this job'))


def flaky(facts):
    """The same input failed once and passed once, with nothing changed between.

    The pair comes from the ledger (`history.flaky`): same input digest, same
    argv, same environment, both attempts reached a verdict, and they disagree.
    A remote input is a frozen snapshot, so it cannot drift; a result that says
    it drifted is refused here anyway, because a changed tree explains a changed
    verdict better than flakiness does. The hint names the failing attempt,
    because that is the one with the evidence worth reading.
    """
    pair = facts.get('flaky')
    if not isinstance(pair, dict) or facts.get('drifted'):
        return None
    words = {'failed-then-passed': 'failed then passed',
             'passed-then-failed': 'passed then failed'}
    shards = [item for item in pair.get('shards') or [] if item.get('order') in words]
    if shards:
        first = shards[0]
        more = (' (and %d more shard%s)' % (len(shards) - 1, '' if len(shards) == 2 else 's')
                if len(shards) > 1 else '')
        return ('shard %s of this input %s with no change%s; treat as flaky, see '
                'pandora result %s' % (first['shard'], words[first['order']], more,
                                       first['failed']))
    if pair.get('order') not in words:
        return None
    return ('this input %s with no change; treat as flaky, see pandora result %s'
            % (words[pair['order']], pair['failed']))


def missing_report(facts):
    """The runner exited and the thing a reader would read is not there.

    Not the same as zero failures, which is the reason the engine records
    `missing` at all. The cure is a local run, because the reason the report was
    not written is in the runner's own output and not in anything Pandora holds.
    """
    if facts.get('outcome') in ('oom', 'timed_out', 'cancelled'):
        return None                      # it was killed; of course nothing was written
    collected = facts.get('collected') or {}
    missing = sorted(path for path, state in collected.items() if state != 'present')
    if not missing:
        return None
    code = facts.get('observed_exit')
    return ('runner exited %s without writing %s; run it locally with PANDORA_OFF=1 to '
            'see why' % ('?' if code is None else code, ', '.join(missing[:3])))


def drifted(facts):
    """`drift = "fail"` refused a verdict because the tree changed under the run.

    Only `fail`: under `warn` the verdict stands and the run's own notice
    already said so, so a hint would be a second copy of the same sentence.
    """
    if not facts.get('drifted') or facts.get('drift') != 'fail':
        return None
    paths = list(facts.get('drift_paths') or [])
    where = ', '.join(paths[:5]) if paths else 'an undetermined path'
    if len(paths) > 5:
        where += ' and %d more' % (len(paths) - 5)
    return 'the worktree changed during the run at %s; re-run it' % where


def gitignored(facts):
    """A path the command named exists here, is ignored, and was not shipped.

    The client's rule, not the engine's: only the Mac has the worktree and the
    manifest that was frozen from it. Run on failure only, over the tail of the
    log, and capped -- see `LOG_TAIL_BYTES` and `MAX_TOKENS`.
    """
    if facts.get('outcome') == 'passed':
        return None
    tail = facts.get('log_tail') or ''
    exists = facts.get('exists')
    ignored = facts.get('ignored')
    if not tail or exists is None or ignored is None:
        return None
    shipped = facts.get('shipped') or frozenset()
    for token in path_tokens(tail):
        if token in shipped or not exists(token) or not ignored(token):
            continue
        return ('%s exists locally but is gitignored, so it was not in the snapshot; '
                'add it to [sync] include' % token)
    return None


# Worst-first, and the order is the contract: a killed run says nothing about a
# report it never got to write.
RULES = (oom, timed_out, drifted, flaky, missing_report, gitignored)


def hint_for(facts):
    """The first rule that fires, or None. Never raises: a hint is a courtesy."""
    for rule in RULES:
        try:
            answer = rule(facts or {})
        except Exception:                           # noqa: BLE001 - never fail a run for a hint
            continue
        if answer:
            return answer
    return None


def facts_from_result(result, **extra):
    """The engine's half of the facts, from a result dictionary it just wrote."""
    evidence = result.get('evidence') or {}
    facts = {
        'outcome': result.get('outcome'),
        'job': result.get('job'),
        'peak_mib': result.get('peak_mib'),
        'ceiling_mib': result.get('ceiling_mib'),
        'reservation_mib': result.get('reservation_mib'),
        'observed_exit': result.get('observed_exit'),
        'wall_seconds': (result.get('durations') or {}).get('execute')
                        or result.get('wall_seconds'),
        'evidence': {key: value for key, value in evidence.items() if key != 'samples'},
        'collected': evidence.get('collected') or {},
        'drifted': result.get('drifted'),
        'drift': result.get('drift'),
        'flaky': result.get('flaky'),
    }
    facts.update(extra)
    return facts
