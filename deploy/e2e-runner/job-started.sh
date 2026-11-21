#!/bin/sh
# The e2e runner's job-started hook. It runs before the job's first step. If the
# check itself cannot run, the runner is killed the way a refusal kills it: a
# hook that only failed would still let `if: always()` steps run.
/usr/bin/python3 -I "$(dirname -- "$0")/trusted_job.py" && exit 0
[ "$(id -u)" -ne 0 ] && kill -s KILL -- -1
kill -s KILL 0
exit 1
