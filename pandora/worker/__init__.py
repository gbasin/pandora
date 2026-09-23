"""Everything about the machine a run lands on, rather than about the run.

`provision` makes a worker; `canary` decides whether it may be used; `status`
says what it is; `gc` keeps it from filling up; `goldens` says what is baked
into it. The halves split by where they execute:

* `versions`, `provision`, `cli` run on the *control* machine and talk to the
  worker over SSH;
* `service`, `canary`, `gc`, `goldens`, `pins` run *on the worker*, shipped
  inside the same content-addressed engine bundle the engine uses, because they
  read cgroups, btrfs qgroups and the Incus socket several times a second.
"""
