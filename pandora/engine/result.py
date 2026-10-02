"""Hints: the one sentence a reader of a failure would have wanted first.

A result already says what happened. A hint says what to *do*, and only when the
evidence already on hand names the action. There are eight rules, and every one
of them is a function of facts that were measured -- a peak against a ceiling, a
wall clock against a limit, a declared report that is not there, two manifests
that differ, two verdicts on one input that disagree, a program the worker does
not have, a path the command named that exists here and was not shipped, a
write-back and what it found.

What this file deliberately is not: a guesser. No model, no pattern library, no
"this looks like a flaky test" -- the flaky rule fires only on two recorded
attempts with the same tree and command that reached opposite verdicts. A rule that cannot point at the measurement it
used does not belong here, because a wrong hint is worse than none -- an agent
acts on it, and then the next twenty minutes are spent on the wrong thing.

The rules are pure functions of one `facts` dictionary so that both sides can
run them. The engine has the outcome, the peak, the ceiling and the collected
outputs, so it attaches a hint at collect time. The client has the worktree, the
snapshot manifest and the log, so it fills in the rules the engine cannot see.
`hint_for` returns the first rule that fires, and order is worst-first: a
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

# Signatures of "the program is not there": Playwright's missing browser, a
# node `spawn` failure, the shell's own wording. Each captures the executable's
# name or path where the wording carries one.
EXEC_MISSING = (
    re.compile(r"Executable doesn't exist(?:\s+at\s+(\S+))?"),
    re.compile(r'\b(?:spawn\w*|exec\w*|launch)\s+([\w./@+-]+)\s+ENOENT'),
    # zsh's order before bash's, or `zsh: command not found: rg` names the shell.
    re.compile(r'command not found:\s*([\w.+-]+)'),
    re.compile(r'([\w.+-]+): command not found'),
)
# An ENOENT line that names a file syscall -- `ENOENT, open 'x'` -- is a missing
# *file*, which is the gitignored rule's case, not a missing program's.
FILE_ACCESS = re.compile(r'ENOENT[^\n]*\b(?:open|scandir|stat|lstat|access|mkdir|'
                         r'unlink|rename|chmod|utime|readlink|symlink|copyfile)\b')
# A line that blames something: the gitignored rule only trusts a path a failing
# line names, not whatever a stack frame happens to mention.
ERRORISH = re.compile(r"cannot find|can't find|enoent|doesn't exist|does not exist|"
                      r'not found|no such file', re.IGNORECASE)


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
    """Killed for memory. The class it ran in, and what the evidence says next.

    Two kills. The kernel's (`oom_kill`) is a run that reached the ceiling. The
    watchdog's (`memory-thrash`) is a run that stalled in reclaim at the
    effective limit, `memory.high`, which is 90% of the ceiling: it never got
    to the ceiling at all, so the hint names the limit that applied. Which kind
    of memory filled the cgroup is said only when the record carries the
    `memory.stat` breakdown; a record without one makes no cause claim.

    The learned-class case comes first for both kills: the oom resets the class
    to the declared one, so nothing in `pandora.toml` needs to change. Otherwise
    the cure names the next class up from the one the run *used*, never the one
    it already has, and says so when there is none.
    """
    if facts.get('outcome') != 'oom':
        return None
    evidence = facts.get('evidence') or {}
    job = facts.get('job') or 'this job'
    thrash = (evidence.get('reason') or '') == 'memory-thrash'
    declared, used = facts.get('size_declared'), facts.get('size_used')
    if declared in ORDER and used in ORDER and ORDER.index(declared) > ORDER.index(used):
        # The worker had learned a smaller class than the repository declared,
        # and the oom resets it: nothing in pandora.toml needs to change.
        if thrash:
            return ("the learned class for job %s was %s and the watchdog killed it at "
                    "that class's limit (%s); declared %s applies again from the next run"
                    % (job, used, thrash_limit(facts, evidence), declared))
        return ('the learned class for job %s was %s (peak %d MiB of a %d MiB ceiling); '
                'declared %s applies again from the next run'
                % (job, used, mib(facts.get('peak_mib')), mib(facts.get('ceiling_mib')),
                   declared))
    if thrash:
        return thrash_hint(facts, evidence, job, used)
    return ('raise the size class for job %s (peak %d MiB of a %d MiB ceiling)'
            % (job, mib(facts.get('peak_mib')), mib(facts.get('ceiling_mib'))))


# Size classes, smallest first (`admission.CLASSES`), for the oom rule.
ORDER = ('small', 'medium', 'large', 'xlarge')
# Above this share of anon + file, one of the two is named as what filled the
# cgroup. Below it the record says both were large, which is also a finding.
DOMINANT = 0.6


def bytes_mib(value):
    return int(value) // 1048576 if isinstance(value, (int, float)) and value > 0 else 0


def thrash_limit(facts, evidence):
    """The limit the watchdog stopped the run at, and how long it stalled there.

    `memory.high` against the ceiling when the record has it (every watchdog
    record does), the effective wall otherwise, and the peak against the
    ceiling only for a record that carries neither.
    """
    ceiling = mib(facts.get('ceiling_mib')) or bytes_mib(evidence.get('memory_max'))
    limit = bytes_mib(evidence.get('memory_high')) or bytes_mib(evidence.get('memory_wall'))
    stalled = evidence.get('thrashing_seconds')
    if limit and ceiling and limit < ceiling:
        text = 'memory.high %d MiB of the %d MiB ceiling' % (limit, ceiling)
    elif limit:
        text = '%d MiB limit' % limit
    else:
        text = 'peak %d MiB of a %d MiB ceiling' % (mib(facts.get('peak_mib')), ceiling)
    if isinstance(stalled, (int, float)) and stalled > 0:
        text += ', stalled %d s' % round(float(stalled))
    return text


def breakdown(evidence):
    """`anon 8450 MiB, file 1492 MiB, ...` from a recorded `memory.stat`, or None."""
    stat = evidence.get('memory_stat')
    if not isinstance(stat, dict):
        return None
    anon, file = stat.get('anon'), stat.get('file')
    if not isinstance(anon, int) or not isinstance(file, int) or anon + file <= 0:
        return None
    return ', '.join('%s %d MiB' % (key, bytes_mib(stat[key]))
                     for key in ('anon', 'file', 'kernel', 'shmem')
                     if isinstance(stat.get(key), int))


def thrash_cause(evidence):
    """What filled the cgroup, from `memory.stat`, or None when it is not recorded."""
    parts = breakdown(evidence)
    if parts is None:
        return None
    anon, file = evidence['memory_stat']['anon'], evidence['memory_stat']['file']
    if anon >= DOMINANT * (anon + file):
        return "%s: mostly anonymous memory, the job's own processes" % parts
    if file >= DOMINANT * (anon + file):
        return '%s: mostly file pages, page cache and shmem' % parts
    return '%s: anonymous memory and file pages in similar shares' % parts


def thrash_summary(result):
    """One line for `pandora result`: the limit a watchdog oom stalled at, and
    the breakdown when the record has one. None for any other result."""
    evidence = (result or {}).get('evidence') or {}
    if result.get('outcome') != 'oom' or evidence.get('reason') != 'memory-thrash':
        return None
    parts = breakdown(evidence)
    return 'watchdog: %s%s' % (thrash_limit(result, evidence),
                               '; %s' % parts if parts else '; no memory breakdown recorded')


def thrash_hint(facts, evidence, job, used):
    """The watchdog's oom at the class the run used, with the next step up."""
    where = ("the %s class's limit" % used) if used in ORDER else 'its memory limit'
    cause = thrash_cause(evidence) or 'no memory breakdown recorded, so the cause is unknown'
    cpus = facts.get('cpus_hint')
    fewer = (('lower its parallelism (it ran with PANDORA_CPUS=%d)' % cpus)
             if isinstance(cpus, int) and cpus > 1 else None)
    if used == ORDER[-1]:
        cure = '%s is the largest class, so %s' % (
            used, ' or '.join(step for step in (fewer, 'split it into shards') if step))
    else:
        bigger = (('declare size = "%s" for job %s in pandora.toml'
                   % (ORDER[ORDER.index(used) + 1], job)) if used in ORDER
                  else 'declare a larger size for job %s in pandora.toml' % job)
        cure = ' or '.join(step for step in (fewer, bigger) if step)
    return ('watchdog killed job %s at %s (%s); %s; %s'
            % (job, where, thrash_limit(facts, evidence), cause, cure))


def timed_out(facts):
    """Killed by the wall clock. The only cure Pandora can name is the wall."""
    if facts.get('outcome') != 'timed_out':
        return None
    seconds = float(facts.get('wall_seconds') or 0)
    return ('timed out after %.0fs; raise timeout_minutes for job %s, or split it into '
            'shards' % (seconds, facts.get('job') or 'this job'))


def flaky(facts):
    """The same command on the same tree failed once and passed once.

    The pair comes from the ledger (`history.flaky`): same tree digest, same
    argv, same environment, both attempts reached a verdict, and they disagree.
    The hint says "this command on this tree", not "this input", because the
    rule compares the command too and `same tree as` does not.
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
        return ('shard %s of this command on this tree %s with no change%s; treat as '
                'flaky, see pandora result %s'
                % (first['shard'], words[first['order']], more, first['failed']))
    if pair.get('order') not in words:
        return None
    return ('this command on this tree %s with no change; treat as flaky, see '
            'pandora result %s'
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


def missing_executable(facts):
    """A program the command needs is not installed on the worker.

    Playwright says "Executable doesn't exist", node says "spawn foo ENOENT",
    the shell says "command not found": the snapshot shipped fine, but the
    thing that was supposed to run it is absent. Ordered before the file rules,
    because such a log also names plenty of paths and `gitignored` would
    otherwise blame whichever one a stack frame mentions first. A bare ENOENT
    that names a file syscall is left for `gitignored` -- that is a missing
    file, and this rule would just be guessing at a name.
    """
    if facts.get('outcome') == 'passed':
        return None
    for line in (facts.get('log_tail') or '').splitlines():
        name = None
        for pattern in EXEC_MISSING:
            match = pattern.search(line)
            if match:
                name = (match.group(1) or '').strip('\'"`,;:)]}').rsplit('/', 1)[-1]
                break
        if name is None:
            if 'ENOENT' not in line or FILE_ACCESS.search(line):
                continue
            name = ''
        which = "'%s'" % name if name else 'a command the run invoked'
        return ('%s is not installed on the worker; install it there, or bake it '
                'into the golden' % which)
    return None


def gitignored(facts):
    """A path the command named exists here, is ignored, and was not shipped.

    The client's rule, not the engine's: only the Mac has the worktree and the
    manifest that was frozen from it. Run on failure only, over the tail of the
    log, and capped -- see `LOG_TAIL_BYTES` and `MAX_TOKENS`. Only lines that
    read as an error are blamed (`ERRORISH`): a stack frame names paths it did
    not miss.
    """
    if facts.get('outcome') == 'passed':
        return None
    tail = facts.get('log_tail') or ''
    exists = facts.get('exists')
    ignored = facts.get('ignored')
    if not tail or exists is None or ignored is None:
        return None
    shipped = facts.get('shipped') or frozenset()
    blamed = '\n'.join(line for line in tail.splitlines() if ERRORISH.search(line))
    for token in path_tokens(blamed):
        if token in shipped or not exists(token) or not ignored(token):
            continue
        return ('%s exists locally but is gitignored, so it was not in the snapshot; '
                'add it to [sync] include' % token)
    return None


def written_back(facts):
    """A `--update` run's write-back: what to review, or why nothing landed.

    The client's rule: only the Mac can compare a proposal with the worktree,
    so the record this reads is filled in after the engine's result arrives.
    Silent when the run did not pass -- the failure is the thing to read -- and
    when the run changed nothing.
    """
    record = facts.get('writeback') or {}
    state = record.get('state')
    if state == 'published':
        count = len(record.get('written') or [])
        return ('review `git diff` of %d updated file%s, then validate without --update'
                % (count, '' if count == 1 else 's'))
    if state == 'conflicted':
        count = len(record.get('conflicts') or [])
        return ('%d declared file%s changed here during the run and kept your version; '
                'merge the worker\'s from %s, then `%s`'
                % (count, '' if count == 1 else 's', record.get('proposed'),
                   record.get('resolve')))
    if state == 'stale':
        paths = list(record.get('stale') or [])
        return ('the worktree changed during the run at %s%s, so nothing was written '
                'back; re-run it with --update'
                % (', '.join(paths[:5]), ' and more' if len(paths) > 5 else ''))
    if state == 'partial':
        return ('write-back stopped partway; %d file(s) landed and the rest are '
                'in %s -- `git diff` shows the seam, then validate without --update'
                % (len(record.get('written') or []), record.get('proposed')))
    if state == 'incomplete':
        return 'nothing was written back: %s' % record.get('why')
    return None


# Worst-first, and the order is the contract: a killed run says nothing about a
# report it never got to write.
RULES = (oom, timed_out, drifted, flaky, missing_executable, missing_report,
         written_back, gitignored)


def hint_named(facts):
    """(rule name, text) for the first rule that fires, or None. Never raises."""
    for rule in RULES:
        try:
            answer = rule(facts or {})
        except Exception:                           # noqa: BLE001 - never fail a run for a hint
            continue
        if answer:
            return rule.__name__, answer
    return None


def hint_for(facts):
    """The first rule that fires, or None. Never raises: a hint is a courtesy."""
    named = hint_named(facts)
    return named[1] if named else None


def facts_from_result(result, **extra):
    """The engine's half of the facts, from a result dictionary it just wrote."""
    evidence = result.get('evidence') or {}
    facts = {
        'outcome': result.get('outcome'),
        'job': result.get('job'),
        'peak_mib': result.get('peak_mib'),
        'ceiling_mib': result.get('ceiling_mib'),
        'reservation_mib': result.get('reservation_mib'),
        'size_declared': result.get('size_declared'),
        'size_used': result.get('size_used'),
        'cpus_hint': result.get('cpus_hint'),
        'observed_exit': result.get('observed_exit'),
        'wall_seconds': (result.get('durations') or {}).get('execute')
                        or result.get('wall_seconds'),
        'evidence': {key: value for key, value in evidence.items() if key != 'samples'},
        'collected': evidence.get('collected') or {},
        'drifted': result.get('drifted'),
        'drift': result.get('drift'),
        'flaky': result.get('flaky'),
        'writeback': result.get('writeback'),
    }
    facts.update(extra)
    return facts
