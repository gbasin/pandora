"""The four shapes of an over-limit run, kept because the watchdog is tuned to them.

The full investigation -- why `memory.max` livelocks in reclaim instead of
OOM-killing, and why no cgroup arrangement fixes it -- is in
`experiments/executor/memtest.py` and `notes/incus-executor-poc-2026-09-21.md`
§4. What the shipped package needs from it is the hog itself: the canary runs
one under a deliberately small ceiling and asserts that the watchdog reaches
`oom` with evidence, which is the regression test for the whole arrangement.

The distinction that matters, and the reason `file` is the canary's hog: an
anonymous-memory overrun is OOM-killed by the kernel in under five seconds and
needs no watchdog at all. A *file-cache* overrun is not, because reclaim
succeeds 99.98% of the time, so the kernel never reaches the OOM killer and the
run simply thrashes forever. That is what a real over-ceiling build looks like.
"""

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
    'file': ['bash', '-c', 'while :; do cat $(find /work/node_modules -type f -size +8k '
                           '| head -20000) > /dev/null 2>&1; done'],
    # Both at once, which is what an over-ceiling build really is.
    'mixed': ['bash', '-c',
              'node -e "const a=[];for(;;){a.push(Buffer.alloc(16*1024*1024).fill(1));}" & '
              'while :; do cat $(find /work/node_modules -type f -size +8k '
              '| head -20000) > /dev/null 2>&1; done'],
}

DEFAULT = 'file'


def hog(name=DEFAULT):
    if name not in HOGS:
        raise KeyError('unknown hog %r; known: %s' % (name, ', '.join(sorted(HOGS))))
    return list(HOGS[name])
