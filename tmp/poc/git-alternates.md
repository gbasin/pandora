# PoC 3: git-alternates — a `.git`-less mirror that still answers `git status`

Goal: simulate Pandora's box-side workspace — a file tree synced WITHOUT `.git` — yet able to run acme's `fingerprint()` (which calls `git rev-parse HEAD`, `git diff HEAD`, `git ls-files --others --exclude-standard`). Trick: a separate gitdir sharing objects with a central bare store via `objects/info/alternates`, plus a gitfile at `mirror/.git` so plain `git` works from cwd.

## Setup

```
mkdir -p /tmp/pandora-poc/git && cd /tmp/pandora-poc/git
git clone --bare /Users/you/code/acme store.git   # 416M, 0.037s (local hardlinks)
```

Mac side (wt-a worktree from PoC 2):
```
cd /tmp/pandora-poc/wt-a && git switch -c poc-branch
echo "poc edit" >> tools/notes/local-validation.md && git add -A && git commit -qm "poc commit"
echo "uncommitted" >> package.json        # uncommitted tracked edit
echo untracked > poc-untracked.txt        # untracked file
git push /tmp/pandora-poc/git/store.git HEAD:refs/pandora/ws1/head
# → pushed bff621d067dd0e63df0dd778f5f08a9e239ddf0b (also uploaded 39 LFS objects, 126 MB — acme uses git-lfs)
```

Box side:
```
mkdir mirror && rsync -a --exclude='.git' /tmp/pandora-poc/wt-a/ mirror/   # the "Mutagen mirror"
git init --bare ws1.git
echo "/tmp/pandora-poc/git/store.git/objects" > ws1.git/objects/info/alternates
git --git-dir=ws1.git config core.bare false
git --git-dir=ws1.git config core.worktree /tmp/pandora-poc/git/mirror
git --git-dir=ws1.git symbolic-ref HEAD refs/heads/poc-branch
git --git-dir=ws1.git update-ref refs/heads/poc-branch bff621d067dd...   # by SHA: alternates share OBJECTS, not refs — `update-ref refs/heads/x refs/pandora/ws1/head` fails with "not a valid SHA1"
git --git-dir=ws1.git --work-tree=mirror read-tree HEAD                  # 4.7k entries
git --git-dir=ws1.git --work-tree=mirror update-index --refresh          # 1.7s; exits 1 listing "package.json: needs update" — expected, that's the uncommitted edit
```

## Verification — mirror vs Mac (identical)

| Check | mirror | wt-a (Mac) |
|---|---|---|
| `git rev-parse HEAD` | bff621d0… | bff621d0… ✓ |
| `git status --porcelain` | ` M package.json` + `?? poc-untracked.txt` | identical ✓ |
| `git log --oneline -3` | poc commit / merge #1277 / #1275 | same ✓ |
| `git branch --show-current` | poc-branch | poc-branch ✓ |
| `git diff HEAD --binary \| md5` | 44979ab064489bc7a318fee9e2c31a76 | 44979ab064489bc7a318fee9e2c31a76 ✓ |

## Isolation — commit in the mirror

```
cd mirror && git commit -qam x   → fac5d6ca x
```
- `store.git for-each-ref`: unchanged; `refs/pandora/ws1/head` still bff621d0.
- Mac repo `git -C wt-a log --oneline -1`: still bff621d0; status unchanged.
- New objects land in `ws1.git/objects` (alternates are a read-fallback, never a write target). Mirror-side commits/objects can never corrupt the shared store. ✓

## Gitfile (required — acme tools call plain `git` in cwd)

```
echo "gitdir: /tmp/pandora-poc/git/ws1.git" > mirror/.git
cd mirror && git rev-parse HEAD        → bff621d0…
git status --porcelain                 → M package.json / ?? poc-untracked.txt
git branch --show-current              → poc-branch
```
Works. One wrinkle: git normally expects `ws1.git` to contain a `commondir`/`gitdir` back-reference — for a hand-built gitdir it works because `core.worktree` is set; `git rev-parse --git-dir` resolves via the gitfile without complaint. **Gotcha**: git validates that the gitdir "belongs" to the worktree only when the gitdir contains a `gitdir` file (created by `git worktree add`/`init --separate-git-dir`); without it, no check — fine. If Pandora instead wants `git worktree`-grade bookkeeping, `git init --separate-git-dir ws1.git mirror` creates the same pair with the back-pointer included — equally viable, one command instead of hand-assembly.

## Gotchas recorded

1. **Alternates share objects, not refs.** Set refs by SHA; resolve `refs/pandora/ws1/head` in store.git first (`git --git-dir=store.git rev-parse refs/pandora/ws1/head`).
2. **`git gc` in store.git can break ws1.** Objects reachable only through ws1's refs are invisible to store's reachability — but our pushed commits ARE reachable via `refs/pandora/ws1/head`, so they're safe as long as pandora keeps a store-side ref per workspace head. Rule: never let a ws gitdir reference objects the store doesn't keep reachable; push creates the ref that pins them. `gitrepository-layout` docs warn: do not run `gc` in a repo whose objects are borrowed unless the borrowing repo's objects are also reachable — the push-ref satisfies this.
3. **LFS**: acme pushes pull 126 MB of LFS objects on first push (39 objects). On the real box, the workspace mirror gets *smudged* files via rsync — no LFS smudge needed — but any `git checkout`/`read-tree`-based refresh on the box would produce pointer files unless `GIT_LFS_SKIP_SMUDGE=1` or lfs is installed. We never checkout on the box (rsync owns files; `read-tree`/`update-index` only touch the index), so it's fine — but document it.
4. **`update-index --refresh` exit code** is 1 when files differ — it doubles as the drift detector: its stdout (`package.json: needs update`) enumerates content-changed tracked files. Cheap consistency check after rsync: refresh must report exactly the worktree's known-dirty set.
5. `git clone --bare` of a local path hardlinks objects — the real box flow (fetch over SSH from origin) differs only in transport; alternates wiring is identical.
6. Worktree `.git` on the Mac is itself a gitfile — rsync must exclude `.git` (done) AND the box gitfile path must be absolute to the box-side gitdir (different path than Mac's — write it at workspace provisioning time, don't sync it).

## Verdict

**WORKS.** A `.git`-less rsync mirror + hand-built gitdir with alternates + gitfile gives acme's `fingerprint()` everything it needs: identical `rev-parse`, `diff`, `status`, `ls-files` results to the Mac worktree, with full write isolation (mirror commits never touch store or Mac). This resolves the critique's blocker #2 without syncing `.git` over rsync and without an acme patch: pandora provisions `ws.git` + `.git` gitfile at workspace create; per-run cost is one `update-ref` + `read-tree` + `update-index --refresh` (~2 s on this repo).
