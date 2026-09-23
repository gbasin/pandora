"""Which infrastructure failures earn one more attempt, and how that is decided.

An `infra_failed` run reached no verdict: the command under test did not say
pass or fail, the machinery around it broke. Some of that breakage is a coin
toss (a clone that raced a pool operation, an instance that vanished, an
engine that restarted under a run) and a second attempt on a fresh instance is
independent of the first. Some of it is a fact that will be true again in a
second (a golden that does not build, a disk quota the golden already
exceeds), and retrying it only doubles the wait for the same exit 70.

So the decision is a table, keyed on a named cause, and not a guess. Three rules
sit above the table and are enforced by the callers, because only they can see
them:

* **Nothing of the command's own output has reached the caller.** A partly
  observed run is not repeatable from the caller's side: it has already read
  half an answer. `pandora:` lines are Pandora talking, not the command, and do
  not count -- see `command_output`.
* **Remote to remote, once.** A retry is a second submission of the same frozen
  input to the same worker. It is never the local lane: that is fallback, and
  fallback ends at `accepted`.
* **Never a cancelled run, never a stale one, never a resource verdict.** An
  `oom` is its own outcome and never reaches this table; a disk quota is a
  resource verdict that arrives as an infra failure and is named here as not
  retryable.
"""
import re

# cause -> (retryable, why). The why is said to the caller when the answer is
# no, so it is written for someone deciding what to do next.
CAUSES = {
    'clone-failed': (True, 'a clone of a golden that exists is cheap and shares nothing '
                           'with the clone that failed'),
    'instance-lost': (True, 'the instance vanished; a fresh one is independent of it'),
    'execution-failed': (True, 'the host could not start or feed the command, so nothing '
                               'of the command ran'),
    'supervisor-gone': (True, 'the engine restarted under the run; that says nothing '
                              'about the input'),
    'prepare-failed': (False, 'a golden that failed to build fails the same way again, '
                              'and a second build costs minutes'),
    'disk-quota': (False, "the run's disk quota refused the clone and would refuse it "
                          'again'),
    'destroy-incomplete': (False, 'the command reached a verdict and a machine is still '
                                  'on the worker; that is for the operator'),
    'partition-unverified': (False, 'the shards ran, and what they reported against the '
                                    'plan will not change on a second pass'),
    'shard-failed': (False, 'a shard failed after its own retry inside the fan-out'),
    'admission-timeout': (False, 'the worker had no room for the whole admission wait, '
                                 'and a retry joins the same queue'),
    'engine-error': (False, 'the engine failed in a way it does not recognise, and an '
                            'unrecognised failure is not retried'),
    # Raised before `accepted`. They never reach a retry: the fallback policy
    # owns them. Listed so that every cause the engine can write has an answer.
    'disk-floor': (False, 'refused before acceptance; the fallback policy decides'),
    'admission-refused': (False, 'refused before acceptance; the fallback policy decides'),
    # The daemon's own, not the engine's: the worker stopped answering while the
    # run was going, so the run may still be executing, and a resubmission would
    # run it twice.
    'worker-lost': (False, 'the worker stopped answering mid-run, so the run may still '
                           'be executing there'),
}

# How an executor exception names itself in `evidence.error`, for results
# written before causes were recorded explicitly.
EXCEPTIONS = {'PrepareFailed': 'prepare-failed', 'CloneFailed': 'clone-failed',
              'ExecutionFailed': 'execution-failed', 'InstanceLost': 'instance-lost'}


def retryable(cause):
    """(bool, why) for one cause. An unknown cause is not retried."""
    return CAUSES.get(cause, (False, 'the cause %r is not one this engine names' % cause))


def cause_of(result):
    """The named cause of an `infra_failed` result, or None for any other outcome.

    Explicit first: the engine writes `evidence.cause` at every site that can
    fail a run. Derived second, for a result written by an engine that did not,
    and `engine-error` last -- never retryable, because a cause nobody named is
    a cause nobody has reasoned about.
    """
    if not isinstance(result, dict) or result.get('outcome') != 'infra_failed':
        return None
    evidence = result.get('evidence') or {}
    if evidence.get('cause') in CAUSES:
        return evidence['cause']
    error = str(evidence.get('error') or '')
    name = error.split(':', 1)[0].strip()
    if name == 'CloneFailed' and 'disk quota' in error:
        return 'disk-quota'
    if name in EXCEPTIONS:
        return EXCEPTIONS[name]
    if 'gone at engine restart' in str(evidence.get('reason') or ''):
        return 'supervisor-gone'
    if evidence.get('destroy_error'):
        return 'destroy-incomplete'
    if evidence.get('capacity'):
        return 'disk-floor'
    if evidence.get('admission'):
        return 'admission-refused'
    verification = evidence.get('verification') or result.get('verification') or {}
    if verification and not verification.get('verified') and not verification.get('unverified'):
        return 'partition-unverified'
    return 'engine-error'


def cause_of_exception(error):
    """The cause for an exception caught in the supervisor."""
    name = type(error).__name__
    if name == 'CloneFailed' and 'disk quota' in str(error):
        return 'disk-quota'
    return EXCEPTIONS.get(name, 'engine-error')


# A line Pandora wrote, optionally under one or more fan-out labels. The labels
# are how `fanout.drain_tails` prefixes a child's lines in its parent's log.
PANDORA_LINE = re.compile(r'^(?:\[[^\]\n]*\] )*pandora: ')
LABELS = re.compile(r'^(?:\[[^\]\n]*\] )*')


def command_output(data):
    """True if any complete line of `data` is the command's own output.

    Blank lines are nobody's. Everything else that is not a `pandora:` line is
    the command talking, and once it has talked the run cannot be repeated
    without the caller seeing two answers to one question.
    """
    if isinstance(data, bytes):
        data = data.decode('utf-8', 'replace')
    for line in data.splitlines():
        if line.strip() and not PANDORA_LINE.match(line):
            return True
    return False


def could_be_pandora(fragment):
    """Whether an unterminated line could still turn out to be a `pandora:` line.

    Used on the tail of a stream that has not ended. A fragment that can no
    longer become one is counted as output at once, rather than held until a
    newline that a daemon restart may mean is never seen.
    """
    if isinstance(fragment, bytes):
        fragment = fragment.decode('utf-8', 'replace')
    if not fragment.strip():
        return True
    rest = LABELS.sub('', fragment, count=1)
    if rest.startswith('[') and ']' not in rest:
        return True                      # a label still being written
    return rest.startswith('pandora: ') or 'pandora: '.startswith(rest)
