---
status: log
---
# Mixed suite recovery on the worker

A synthetic suite recovery fixture exercised the actual Linux worker's cleanup
tools. It represented a parent with a valid failed child, a child whose result
was lost, and a reserved child that had never been staged. These were constructed
recovery records, not executed journey results.

Parent `e944a51db4c972c078ee924252fd5b37` retained the older copied suite cleanup
helper from before operator acknowledgements. The missing-result child,
`cf3d29bfcfb77294dff9c4c42be2883e`, owned a real created Docker container and
network. Child `025a347b37b670e3ec76643b25b8ab19` carried the fixture's valid
nonzero terminal. Reserved identity `dd0faa23d586afb46299bd8b488c9e65` had no
directory or resource.

The operator command held the exclusive worker lock, cleaned the lost child's
resources, acknowledged that child, and ran parent cleanup through the current
operator helper bundle. It retained the original copied helper unchanged. The
parent closed as `infrastructure-failed`, with a submission-bound acknowledgement.
It did not fabricate either missing terminal or create the unstaged child. The
valid failed child's terminal was unchanged. Repeating the operator command
returned the same acknowledgement.

The retained helper SHA-256 was
`ee90cdabaa99a3b4429fbd8c338d8c582cd3ce6b9a6ed4c8412d9e7c7f65db09`.
The local result is
`~/.local/state/pandora/v01-operator-trial/suite-proof.json`.

This supplements the real focused-journey worker-loss trial. It specifically
tests the mixed-child and older-helper compatibility path on the real Docker
host, rather than claiming that a full suite was killed during execution.
