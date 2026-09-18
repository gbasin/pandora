# PoC 1: Mutagen local↔local sync

Mutagen version: **0.18.1** (`mutagen version` → `0.18.1`), installed via `brew install mutagen-io/mutagen/mutagen` (required `brew trust mutagen-io/mutagen` first on this machine). Daemon started with `mutagen daemon start`.

Test fixture: `/tmp/pandora-poc/mut/alpha` — a git repo with 50 tracked files, `.gitignore` = `node_modules/` + `tmp/`, plus ignored files present (`node_modules/dep/x.js`, `tmp/scratch.log`). Beta = empty `/tmp/pandora-poc/mut/beta`.

## (a) Does Mutagen honor `.gitignore` on its own? **NO.**

```
mutagen sync create --name=poc-naive --sync-mode=two-way-resolved alpha beta
```
Result after ~3s: `beta/node_modules/dep/x.js` and `beta/tmp/scratch.log` **present**. Mutagen synced 130 files including all ignored content. `.gitignore` is never read. There is a `--ignore-vcs` flag (for `.git`, `.svn`, `.hg` — the session reports `Ignore VCS mode: Default (Propagate)`) but nothing that parses repo ignore files. Ignore rules must be passed explicitly at create time via `--ignore`.

## (b) `--ignore` from `git ls-files -o -i --exclude-standard --directory`: **WORKS.**

```
$ git ls-files -o -i --exclude-standard --directory   # in alpha
node_modules/
tmp/
$ mutagen sync create --name=poc-ign --sync-mode=two-way-resolved \
    --ignore=node_modules --ignore=tmp alpha beta
$ ls beta/node_modules beta/tmp
ls: beta/node_modules: No such file or directory
ls: beta/tmp: No such file or directory
$ ls beta | wc -l   # 50 files + .gitignore + .git dirs
```

Caveat: `git ls-files -o -i --directory` emits directory names (`node_modules/`). Mutagen `--ignore` accepts both literal paths and glob patterns; passing the directory names works. For Pandora's sync scope (`git ls-files -co --exclude-standard` = everything not ignored) the inverse mapping is clean: everything `git ls-files` doesn't list and isn't `.git` goes into `--ignore`, or simpler — pass the gitignore-derived directory list. Note `--ignore` patterns match against paths relative to the session root; `git check-ignore`-style per-dir patterns may need translation for nested `.gitignore` files (not tested — alpha had one root `.gitignore`).

## (c) `mutagen sync flush` blocks until converged: **WORKS.**

```
echo change >> alpha/file1.txt
time mutagen sync flush poc-ign     → real 0m0.012s ; diff alpha/file1.txt beta/file1.txt → identical
for i in 51..250: create alpha/new$i.txt   (200 files)
time mutagen sync flush poc-ign     → real 0m0.057s ; ls beta | wc -l → 250
```

Flush forces a cycle and returns after it completes. Local↔local timings are near-zero; over SSH add RTT + scan cost, but the blocking semantics are confirmed. **Critical caveat**: flush exit code does NOT report conflicts — with an unresolved conflict `flush` still exited 0. Convergence checks must parse `sync list`.

## (d) Conflict behavior

`two-way-resolved` (alpha wins):
```
mutagen sync pause poc-ign
echo ALPHA-WINS > alpha/file2.txt; echo BETA-LOSES > beta/file2.txt
mutagen sync resume poc-ign && mutagen sync flush poc-ign
→ alpha: ALPHA-WINS ; beta: ALPHA-WINS
```
Beta's edit was **silently discarded**; `sync list` shows no conflict entry. This is the mode Pandora wants for Mac→head (local agent's tree is authoritative), but note the discard is silent — a beta-side write inside a run is erased with no trace.

`two-way-safe`:
```
echo SAFE-ALPHA > alpha/file3.txt; echo SAFE-BETA > beta2/file3.txt
→ both sides keep their content; mutagen sync list shows "Conflicts: 1"
```
`sync list -l` details the conflict:
```
Conflicts:
	(alpha) file3.txt (File (532908e8...) -> File (ec7383d6...))
	(beta)  file3.txt (File (532908e8...) -> File (32a669a6...))
Status: Watching for changes
```
Session stays `Watching for changes` — a conflict does not stall unrelated files (250-file batch synced fine alongside it; unverified whether a persistent conflict blocks later edits to the *same* path — likely until resolved).

## (e) `sync list` shape for convergence/disconnect checks

- `Connected: Yes/No` per endpoint (alpha/beta blocks).
- `Conflicts: N` line appears only when N>0 in short format; `-l` lists paths.
- `Status:` is the session-level state (`Watching for changes`, `Synchronizing`, `Paused`, `Disconnected`, `Halted on ...`).
- Programmatic check: `mutagen sync list <name>` + grep, or `--template` (Go template flag exists on `list`); `mutagen sync monitor` streams transitions.
- Disconnected state untestable with local↔local endpoints (they can't disconnect); over SSH expect `Connected: No` + `Status: Waiting to reconnect`-style.

A reliable "mirror converged" predicate: `flush` returns 0 **AND** `sync list` shows no `Conflicts:` line AND both `Connected: Yes`.

## (f) Update ignores on a live session? **NO — terminate + recreate required.**

`mutagen sync` subcommands: create, list, monitor, flush, pause, resume, reset, terminate. There is no update/reconfigure. `sync list -l` shows ignores as fixed session configuration. Changing `.pandora.toml` ignore rules or the gitignore-derived set → terminate and recreate (cheap: session creation + initial scan was <3s for 130 files; rescan cost on a real repo = one full hash scan).

## Verdict

**WORKS WITH CAVEAT.** Mutagen gives Pandora blocking flush, arbitrary ignores, and authoritative one-direction semantics — but: (1) `.gitignore` is not honored natively; ignore set must be generated (`git ls-files -o -i --exclude-standard --directory` or equivalent) and baked into `--ignore` at create time; (2) ignore changes need session recreate; (3) `flush` exit 0 ≠ converged — must also check `Conflicts`/`Connected` in `sync list`; (4) `two-way-resolved` discards beta edits silently — correct for Mac→head, but anything the box writes back (journey fixture updates, artifacts) must come back through a separate path (rsync-back), not this session.
