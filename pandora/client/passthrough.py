"""Run an unclaimed-but-heavy command locally and write down that it happened.

The owner's complaint this answers: there is currently no way to see what is
still running on the Mac.  Routing only reports what it took; nothing reports
what it declined.  Every command the marker lists under ``heavy`` that Pandora
does *not* claim goes through here, which costs one extra process on a command
that already costs minutes.

Exit fidelity matters more than the log.  A signal death is reproduced as a
signal death, not as exit 128+n, so a caller's `$?` and any `trap` behave as if
the shim were not there.

A placement override (`PANDORA_WHERE`) on a command nobody claims is ignored --
there is no lane to put it in -- but it is written down, because an agent that
keeps asking for a placement Pandora cannot give is something `pandora stats`
should show. The POSIX shim sends a light command here only when the variable
is set, with `--reason override-ignored`, so the "local, not routed" table
still counts heavy commands only.
"""
import argparse
import os
import signal
import subprocess
import sys
import time

from ..exits import USAGE
from . import fallback, placement


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real', required=True)
    parser.add_argument('--repo', default='')
    parser.add_argument('--state', default=None)
    parser.add_argument('--reason', default='unclaimed')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    try:
        where = placement.parse(os.environ.get(placement.ENV))
    except ValueError as error:
        sys.stderr.write('pandora: %s\n' % error)
        return USAGE
    state = args.state or os.environ.get('PANDORA_STATE')
    environment = dict(os.environ, PANDORA_ROUTE_DEPTH='1')
    started = time.time()
    try:
        child = subprocess.Popen([args.real, *command], env=environment)
    except OSError as error:
        sys.stderr.write('pandora: cannot run %s: %s\n' % (args.real, error))
        return 127
    forward = lambda number, _frame: child.send_signal(number)  # noqa: E731
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(number, forward)
    status = child.wait()
    if state:
        try:
            entry = {'ts': started, 'kind': 'passthrough', 'reason': args.reason,
                     'argv': command, 'cwd': os.getcwd(), 'repo': args.repo,
                     'duration_ms': int((time.time() - started) * 1000), 'exit': status}
            if where:
                entry['override'] = where
            fallback.record(state, entry)
        except OSError:
            pass
    if status < 0:                        # died by signal: die the same way
        number = -status
        try:
            signal.signal(number, signal.SIG_DFL)
        except (OSError, ValueError, RuntimeError):
            pass
        os.kill(os.getpid(), number)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
