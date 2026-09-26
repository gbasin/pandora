# PoC 2: pnpm-relink — clone node_modules to a new path, cheap revalidate

Hypothesis: `node_modules` installed at path A, cloned to path B (identical checkout), can be made valid for B by `pnpm install --frozen-lockfile --offline` in seconds, after which acme's `tools/check-worktree-deps.mjs` passes at B.

Setup:
```
cd /Users/you/code/acme && git fetch origin
git worktree add /tmp/pandora-poc/wt-a origin/main --detach   # HEAD 8732fdab
git worktree add /tmp/pandora-poc/wt-b origin/main --detach
```
pnpm: **v12.3.4** via corepack (`packageManager` field in package.json; `corepack enable` then `pnpm` resolves the pin). Node v25.4.0.

## Results

| Step | Command | Time |
|---|---|---|
| Install at A | `pnpm install --frozen-lockfile` | **7.3 s** ("Done in 7.3s", 1165 packages, warm store) |
| Clone A→B | `cp -c -R` × 19 top-level `node_modules` dirs | **11.7 s** real |
| Pre-check at B | `node tools/check-worktree-deps.mjs` | **FAILS** (see below) |
| Relink at B | `pnpm install --frozen-lockfile --offline` | **0.056 s** ("Done in 56ms") |
| Post-check at B | `node tools/check-worktree-deps.mjs` | **PASSES** (exit 0) |

### node_modules stats (wt-a)
- `du -sh node_modules` → **1.3 G**
- Top-level `node_modules` dirs in the workspace (pruned find): **19** (one per workspace package + root; earlier unpruned count of 1405 included nested dirs inside `.pnpm`)
- Symlinks: **4057 total, 0 absolute** — every symlink is relative (`find node_modules -type l -exec readlink {} + | grep -c '^/'` → 0). Hardlinked package contents come from the pnpm store; nothing inside node_modules references path A.

### Evidence

Pre-relink check at B:
```
$ node tools/check-worktree-deps.mjs
This worktree is not validation-ready. Dependency metadata does not identify
a complete installation for this worktree. Run `pnpm install --frozen-lockfile`
from /private/tmp/pandora-poc/wt-b, then retry.
(exit 1)
```
This is `installationBelongsToRoot` failing exactly as predicted: `.pnpm-workspace-state-v1.json` still keyed on `/private/tmp/pandora-poc/wt-a`.

Relink:
```
$ time pnpm install --frozen-lockfile --offline
Scope: all 19 workspace projects
✓ Lockfile passes supply-chain policies (verified 40s ago)
Lockfile is up to date, resolution step is skipped
Done in 56ms using pnpm v12.3.4
```
Post-relink state file:
```
projects keys → ['/private/tmp/pandora-poc/wt-b', '.../wt-b/apps/agent', ...]
```
pnpm rewrote the workspace-state file in place (it does not need to touch package contents — hardlinks + relative symlinks are already valid at the new path).

Function check at B:
```
$ pnpm exec node -e "require.resolve('typescript')"
→ /private/tmp/pandora-poc/wt-b/node_modules/.pnpm/typescript@5.9.3/...
$ pnpm lint
$ oxlint apps packages tools
Found 0 warnings and 0 errors. Finished in 157ms on 1643 files
```

## Caveats

- The 56 ms relink only rewrites the workspace-state bookkeeping because lockfile, package set, and store contents are unchanged. It is not a general "repair arbitrary node_modules" — it works precisely because the clone is exact. On btrfs (remote box) the equivalent of `cp -c` is a snapshot; same premise holds.
- If the destination's lockfile differs from the source's, `--offline` relink will fail or do more work — untested; Pandora's prepare step handles that case with a real install.
- `--offline` requires every package already in the store (true here since wt-a populated it). On the box, the shared `PNPM_STORE_DIR` provides the same guarantee after base install.
- macOS `cp -c` (clonefile) is the APFS analogue of a btrfs snapshot — near-free. 11.7 s for 1.3 GB is still real copy work at the syscall level (clonefile per file); a btrfs `subvolume snapshot` is O(1) and faster.

## Verdict

**WORKS.** Clone 1.3 GB in ~12 s (APFS; O(1) on btrfs) + 56 ms `--offline` install rewrites `.pnpm-workspace-state-v1.json` to the new path, `check-worktree-deps.mjs` passes, and the tree is fully functional (lint ran). **This is a third fix option for the critique's blocker #1**: instead of bind-mounting a stable path, Pandora's prepare step can snapshot-then-`pnpm install --frozen-lockfile --offline` inside the run path — sub-second, no mount privileges needed. Bind-mounting is still more robust for *other* path-sensitive state, but for acme's gate specifically the relink suffices.
