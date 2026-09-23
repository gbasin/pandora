# experiments/executor

An executor driver POC: one Incus system container per run, cloned
copy-on-write from a warm golden instance.

| File | What it is |
| --- | --- |
| `interface.py` | the six-operation seam and its typed errors |
| `incus_driver.py` | the seam implemented over the `incus` CLI, on the worker |
| `admission.py` | memory-admitted scheduling: learned reservation, class ceiling |
| `setup.sh` | host prep: btrfs pool on a loop file, bridge, project `pandora` |
| `poc.py` | golden build, source-injection measurements, one run end to end |
| `memtest.py` | the memory-limit investigation |
| `bench.py` | concurrency and CPU-soft measurements |
| `canary.py` | what would gate a worker image rebuild |
| `test_admission.py`, `test_incus_driver.py` | `python3 -m unittest` |

The driver runs **on the worker**, not over SSH: it samples the instance
cgroup twice a second and polls a log file at the same rate, and an SSH round
trip on this host is ~90 ms. Ship this directory to the worker and call it
there.

    rsync -az experiments/executor/ worker:~/incus-exec/driver/
    ssh worker 'bash ~/incus-exec/driver/setup.sh install && bash ~/incus-exec/driver/setup.sh init'
    ssh worker 'cd ~/incus-exec/driver && python3 poc.py golden && python3 canary.py'

Measurements: `poc.py inject`, `memtest.py repro <variant> <cap_mib> <seconds> <hog>`,
`memtest.py watchdog|neighbour`, `bench.py conc <N> [cpus_hint] [tag] [--force]`,
`bench.py mixed [cpu_weight_for_the_heavy_job]`.

Findings are in `notes/incus-executor-poc-2026-09-21.md`.
