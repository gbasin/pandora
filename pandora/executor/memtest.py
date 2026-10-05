"""The four shapes of an over-limit run, kept because the watchdog is tuned to them.

The full investigation -- why `memory.max` livelocks in reclaim instead of
OOM-killing, and why no cgroup arrangement fixes it -- is in
`experiments/executor/memtest.py` and `notes/incus-executor-poc-2026-09-21.md`
§4. What the shipped package needs from it is the hog itself: the canary runs
one under a deliberately small ceiling and asserts that the watchdog reaches
`oom` with evidence, which is the regression test for the whole arrangement.

The distinction that matters, and the reason `file` is the canary's hog: the
`anon` hog below grows without bound, reaches `memory.max` and is OOM-killed by
the kernel in under five seconds, so it needs no watchdog at all. A *file-cache*
overrun is not, because reclaim succeeds 99.98% of the time, so the kernel
never reaches the OOM killer and the run simply thrashes forever. That makes
`file` the hog that proves the watchdog.

It does not make every watchdog kill a file-cache overrun. A real build whose
anonymous memory settles between `memory.high` and `memory.max` is not killed
by the kernel either: it is throttled at `memory.high`, reclaim evicts its file
pages because anonymous pages have no swap to go to, and it thrashes the same
way. Measured on an eichler `check` (gbasin/pandora#193): anon 8450 MiB, file
1492 MiB. Which one filled the cgroup is in the verdict's `memory_stat`
breakdown, not in the fact that the watchdog fired.

`file` brings its own working set rather than reading the repository's. It
once read `/work/node_modules`, so a golden from a repository without one (any
non-Node toolchain) gave it nothing to read: it spun for the whole wall
without pressure and the canary failed its three oom checks. It now writes
`WORKING_SET_FILES` files of `WORKING_SET_MIB` MiB under `WORKING_SET_DIR`,
three times the canary's 512 MiB ceiling, then reads them round and round.
The files come from `/dev/urandom` so no compression or dedup shrinks them,
and are written with `O_DIRECT` (or `fsync` per file where that is refused)
so writing them does not itself fill the cgroup with dirty pages: buffered
writes under a hard cap with no swap were OOM-killed by the kernel in a
512 MiB test container, which would pass the check without proving the
watchdog. They live under `/work`, not `/tmp`: Ubuntu mounts `/tmp` as tmpfs,
whose pages are shared memory that no reclaim can evict without swap, so a
working set there is an `anon` hog. Nothing cleans it up; the instance is
destroyed.
"""

WORKING_SET_DIR = '/work/.pandora-hog'
WORKING_SET_FILES = 96
WORKING_SET_MIB = 16

# Generate first, then thrash: the clock the canary holds against 60 s starts
# before this runs, and writing 1.5 GiB of urandom takes seconds, not tens.
WORKING_SET = (
    'mkdir -p {dir} && for i in $(seq 1 {files}); do '
    'dd if=/dev/urandom of={dir}/$i bs=1M count={mib} oflag=direct status=none 2>/dev/null '
    '|| dd if=/dev/urandom of={dir}/$i bs=1M count={mib} conv=fsync status=none; done; '
).format(dir=WORKING_SET_DIR, files=WORKING_SET_FILES, mib=WORKING_SET_MIB)
THRASH = 'while :; do cat %s/* > /dev/null 2>&1; done' % WORKING_SET_DIR

HOGS = {
    # `Buffer.alloc(n)` for a large n is a calloc of fresh mmap: the pages are
    # never written, so this grows address space and not charge. It does not OOM
    # and it must not be mistaken for a test of the ceiling.
    'address-space': ['node', '-e', 'const a=[];for(;;){a.push(Buffer.alloc(64*1024*1024));}'],
    # The same loop with the pages touched: real anonymous demand. The kernel
    # kills this in under 5 s by itself.
    'anon': ['node', '-e', 'const a=[];for(;;){a.push(Buffer.alloc(64*1024*1024).fill(1));}'],
    # A working set of file pages several times the cap, read round and round.
    # Reclaim always succeeds, so the charge never fails, so nothing dies. This
    # is the one the watchdog exists for.
    'file': ['bash', '-c', WORKING_SET + THRASH],
    # Both at once: anonymous growth that squeezes a file working set. The
    # working set is written before the growth starts, so the kernel does not
    # kill the writer first.
    'mixed': ['bash', '-c',
              WORKING_SET +
              'node -e "const a=[];for(;;){a.push(Buffer.alloc(16*1024*1024).fill(1));}" & '
              + THRASH],
}

DEFAULT = 'file'


def hog(name=DEFAULT):
    if name not in HOGS:
        raise KeyError('unknown hog %r; known: %s' % (name, ', '.join(sorted(HOGS))))
    return list(HOGS[name])
