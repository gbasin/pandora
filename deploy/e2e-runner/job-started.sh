#!/bin/sh
# The e2e runner's job-started hook. It runs before the job's first step. If the
# check itself cannot run, the runner is killed the way a refusal kills it: a
# hook that only failed would still let `if: always()` steps run. The paths are
# absolute so the job's environment cannot pick the interpreter.
PATH=/usr/bin:/bin
export PATH
/usr/bin/python3 -I /usr/local/libexec/pandora-e2e/trusted_job.py && exit 0
[ "$(/usr/bin/id -u)" -ne 0 ] && kill -s KILL -- -1
kill -s KILL 0
exit 1
