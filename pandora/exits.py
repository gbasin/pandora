"""Exit codes, in one place, because three processes must agree on them.

The rule behind the table: Pandora never invents a passing exit. A code here is
either the command's own, or one of these, and each of these means the command's
verdict is unknown rather than good.
"""

USAGE = 64          # a claimed command typed where it cannot be routed as typed
INFRA = 70          # accepted, then Pandora could not finish or find the run
STALE = 75          # a duplicate or expired run, or the fallback budget is full
UNAUTHORIZED = 77   # peer credential or token check failed
STILL_RUNNING = 124  # --max-wait elapsed with the run still going
CANCELLED = 130     # SIGINT: cancelled, and the worker confirmed it

# Pre-accept error codes. Every one of these is provably non-executing, so the
# client is free to run the command locally instead.
PRE_ACCEPT = ('worker-unreachable', 'queue-timeout', 'admission-refused',
              'version', 'unauthorized', 'unenrolled', 'rejected', 'not-claimed',
              'daemon-unreachable', 'daemon-closed', 'handshake-timeout',
              'snapshot-failed', 'transfer-failed')
