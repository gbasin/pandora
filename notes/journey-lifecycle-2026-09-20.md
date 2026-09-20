---
status: log
---
# Journey lifecycle trace, 2026-09-20

The existing surface flow has one owned container. Applying it unchanged to a
service-backed journey leaves three sidecars and a network outside its cleanup
predicate. A stopped runner is not sufficient evidence of cleanup.

Concrete state trace for the integrated profile:

- t0: client={active:A}, worker={lease:held, resources:[]}, systemd={stopHook:installed}.
- t1: worker={intent:A, resources:[]}; `service-cleanup.pending` is durable before creation.
- t2: Docker accepts network and runner creation; worker={intent:A, resources:[network,runner]}.
- t3: Docker accepts database creation but the client loses the acknowledgement.
  worker={intent:A, acknowledged:[network,runner]}, Docker={network,runner,db}.
  Cleanup uses all reserved names, not the acknowledgement list.
- t4a, ordinary failure/cancel: worker removes proxy,pool,db,runner and network,
  verifies both Docker inventories, then removes the pending marker. Only then
  may terminal.cleanup_verified become true and the client clear its request.
- t4b, SIGKILL: worker cannot publish terminal. systemd runs ExecStopPost using
  the same intent and cleanup routine. client={active:A,terminal:absent}; cleanup
  does not invent a test result. Recovery stays unresolved for operator review.
- t4c, client/SSH loss: worker remains alive under systemd; client retry follows
  A. No source resubmission or replacement occurs.
- t5, success: worker={resources:[],terminal:A}; client verifies report checksums
  and source identity. Journey reports stay under the printed evidence directory.
  Surface output publication does not run for a journey.

The systemd overall deadline also invokes the stop hook. A separate 20-minute
service timer requests worker stop. A host reboot or unavailable Docker daemon
can prevent immediate cleanup; absence of verified cleanup never means success.
Agent trials must not be described as passing until their actual evidence and
source diffs have been inspected.
