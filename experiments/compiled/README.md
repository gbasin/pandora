# Compiled application evaluation

Use a disposable, bootstrapped Eichler worktree. The preparation script adds three
untracked files and prints an external routing profile. It refuses to overwrite
existing trial inputs.

```sh
python3 /path/to/pandora/experiments/compiled/prepare.py /path/to/eichler-worktree > /tmp/compiled-profile.json
cd /path/to/eichler-worktree
python3 /path/to/pandora/experiments/routing/launch.py \
  --host ubuntu@WORKER_IP --state /path/to/trial-state \
  --docker-profile /tmp/compiled-profile.json -- \
  docker build -f Pandora.Dockerfile -t compiled:test .
python3 /path/to/pandora/experiments/routing/launch.py \
  --host ubuntu@WORKER_IP --state /path/to/trial-state \
  --docker-profile /tmp/compiled-profile.json -- \
  docker run --rm compiled:test
```

The Dockerfile compiles the real Ike Home web application with Vite. It copies
installation inputs before the dependency install, reuses a pnpm cache mount,
and overlays source without changing installation-input timestamps. The final
image contains the compiled output and a verification script. No project changes
need to land in Eichler. The experiment runs no development server.

Compare a first build, an identical rebuild, an HTML source edit, a dependency
change with its lockfile, and a pinned pnpm version change. Run each resulting
image separately. Supply `node check.cjs EXPECTED_MARKER` after the image tag to
assert that a source edit reached the compiled HTML. Check the returned files at
`apps/borrower-web/dist`; previous generations stay in Pandora's local evidence.

BuildKit reports cached steps in each attempt's stderr log. Its metadata records
resolved base images. Measure both worker execution and total request time:
short warm builds can spend more time transferring and returning evidence than
compiling. A passing output check proves artifact shape and the specified marker;
it does not replace browser behavior tests or the full repository validation.
