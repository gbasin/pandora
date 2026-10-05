# Signed verdicts: runbook for a consuming repository

This runbook is for the owner of a repository that routes a job through
Pandora and wants CI to skip that job when Pandora already ran it. The
Pandora-internal references are [docs/worker.md](worker.md#verdicts) (signing),
[docs/pandora-toml.md](pandora-toml.md#signed-verdicts) (the `[verdicts]`
table) and [docs/operations.md](operations.md#signed-verdict-publication)
(publication and the run record).

## Contents

* [What a verdict is](#what-a-verdict-is)
* [Enable verdicts in a repository](#enable-verdicts-in-a-repository)
* [Roll out](#roll-out)
* [Rotate the key after a worker rebuild](#rotate-the-key-after-a-worker-rebuild)
* [Triage](#triage)
* [Trust rules](#trust-rules)
* [What a verdict does not check](#what-a-verdict-does-not-check)
* [Pruning](#pruning)

## What a verdict is

1. A worker signs a verdict when a whole run of a job passes on a `ready` worker.
2. The verdict names the git tree the run saw, the job, the argv and the golden.
3. The key is an Ed25519 key that never leaves the worker.
4. The client daemon pushes the verdict to `refs/pandora/verdicts/<tree>/<job>` on `origin`.
5. A CI step checks the signature and the fields, and lets later steps skip the job.

## Enable verdicts in a repository

### 1. Declare the job and opt in to publication

The job must declare `git = "synthetic"`, so the worker knows the tree. It must
declare `args = "none"`, so the argv in the verdict is always the argv CI
expects. Add the `[verdicts]` table:

```toml
[[jobs]]
id = "check"
where = "remote"
git = "synthetic"
args = "none"
forms = [{ prefix = ["check"] }]
run = { argv = ["pnpm", "check"] }

[verdicts]
publish = true
```

`remote` defaults to `origin`. Every client Mac must push to that remote
without a prompt. Git runs with `BatchMode=yes`, so a remote that asks for a
password or a host key fails. Do not merge this file before the rollout
condition in [Roll out](#roll-out) holds.

### 2. Add the signers file

Create `.github/pandora/allowed_signers` on the default branch. Write one key
per line, in this format:

```
pandora-verdict namespaces="pandora-verdict" ssh-ed25519 AAAA... pandora-verdict
```

The principal and the namespace are both `pandora-verdict`. Get the key from
the worker:

1. Run `pandora worker status`.
2. Copy the key line after `verdict signer:`.
3. Prefix it with `pandora-verdict namespaces="pandora-verdict" `.

If `status` prints `none yet`, run the job once through Pandora. The first
signed run creates the key. Lines that start with `#` are comments. A file with
no key line verifies nothing (`reason=no_signers`).

### 3. Add the verdict step to the CI job

Add the step before the steps it covers. Pin the action to a full commit sha
of a Pandora release, and put the release tag in a comment:

```yaml
      - uses: actions/checkout@v4
      - name: pandora verdict
        id: verdict
        uses: gbasin/pandora/.github/actions/pandora-verdict@<full sha> # vX.Y.Z
        with:
          job: check
          argv: '["pnpm","check"]'
```

* `job` is the `pandora.toml` job id.
* `argv` is the job's `run.argv` as a JSON array. It must match the signed
  argv exactly.
* `signers` is optional. The default is `.github/pandora/allowed_signers`.
* The verify script never fails the step. Every miss sets `verified` to
  `false` and the covered steps run.
* GitHub downloads the action in "Set up job", before any step runs. If it
  cannot fetch the Pandora repository, the job fails there, as with any other
  `uses:` that cannot be fetched. `continue-on-error` does not cover that
  phase. Rerun the job when GitHub can reach the repository.
* `[verdicts] remote` in `pandora.toml` can name another remote. The verify
  step and the prune workflow read only `origin`. Keep `remote` at `origin`,
  or the step never finds a verdict.
* A shallow checkout is enough. The action fetches what it needs at depth 1.

### 4. Make the covered steps conditional

Add this `if:` to each step that the verdict replaces:

```yaml
      - name: check
        if: steps.verdict.outputs.verified != 'true'
        run: pnpm check
```

Compare with `!= 'true'`. An empty output then runs the step. Do not put the
`if:` on steps the verdict does not cover. A step that already has an `if:`
joins both conditions with `&&`.

### 5. Let Dependabot bump the sha

Add the `github-actions` ecosystem to `.github/dependabot.yml`:

```yaml
version: 2
updates:
  - package-ecosystem: github-actions
    directory: /
    schedule:
      interval: weekly
```

Dependabot updates the sha and the `# vX.Y.Z` comment together. It also bumps
the reusable prune workflow in [Pruning](#pruning).

### 6. Tell the agents

Paste this paragraph into the repository's agent instructions. Replace the
command with the claimed command:

> Before you push, rebase onto `origin/main`, commit every change, and leave
> no untracked files. Then run `pnpm check` through Pandora as the last thing
> you do. Push right after it passes, with no further edits. The run's tree
> includes untracked files, and CI checks the tree of the merge commit it
> builds. So the verdict counts only when your branch already contains
> `origin/main` and the pushed commit holds exactly the files the run saw.

The same paragraph is in [docs/agents-paragraphs.md](agents-paragraphs.md).

## Roll out

1. Upgrade every client Mac that works in the repository to Pandora v0.3.12
   or newer: `pandora upgrade`. `pandora --version` prints the installed
   version, for example `pandora 0.3.12`.
2. Merge the `[verdicts]` table only after step 1 is done on every Mac. A
   daemon older than v0.3.12 does not know the table. It refuses the file, and
   every claimed command exits 70 until that Mac upgrades.
3. Merge the signers file.
4. Add the verdict step without the `if:` on the covered steps. The job runs
   as before, and the step only reports.
5. For a few days, read the `::notice::pandora verdict:` line in each run.
   `verified run <id> for <job> ...; match` is a hit. `not verified for
   <job>: <reason>` is a miss. Look up each miss reason in [Triage](#triage).
6. When the hits are real and the misses are explained, add the `if:` to the
   covered steps.

To back out, remove the `if:` lines. The job then runs on every push again.

## Rotate the key after a worker rebuild

A rebuilt worker, or a new engine root, makes a new key. Verdicts signed with
the old key stop verifying. That is intended.

1. Run `pandora worker status` against the new worker.
2. Copy the line after `verdict signer:`.
3. Add it to `.github/pandora/allowed_signers` with the
   `pandora-verdict namespaces="pandora-verdict" ` prefix.
4. Remove the line of the old key.
5. Merge the change into the default branch.

The change takes effect for every check that starts after the merge. A change
on any other branch has no effect. Until the merge, checks answer
`bad_signature` and the covered steps run.

To revoke a key without a replacement, do steps 4 and 5 only.

## Triage

The step prints one `::notice::` line and sets `verified` and `reason`. Every
miss means the covered steps run. No miss fails the job.

The script checks in a fixed order and reports the first failure. It looks for
the verdict ref before it reads the signers file or parses the `argv` input.
So `no_verdict` hides `signers_missing` and a malformed `argv` (`bad_argv`).
Fix a `no_verdict` first, then rerun to see the next reason.

| `reason` | Cause | Action |
|---|---|---|
| `match` | A signed verdict for this tree, job and argv verified. | None. |
| `no_verdict` | No ref `refs/pandora/verdicts/<tree>/<job>` on `origin`, or the fetch failed. Most often: the branch is behind its base, so the merge commit's tree differs; the agent edited after the run; an untracked file was in the worktree during the run; the run failed or was not routed; or publication failed. | Check `pandora result <id>` on the Mac for the `verdict:` line, and `pandora logs <id>` for the publication line. Rebase onto the base before the final run. |
| `signers_missing` | The signers file is not on the default branch at the `signers` path. | Merge the signers file into the default branch. |
| `no_signers` | The signers file has no key line, or the `signers` input is empty. | Add the worker's key line. |
| `bad_signature` | The signature does not verify against any listed key. The worker was rebuilt, or the key line is wrong. | Rotate the key ([Rotate the key](#rotate-the-key-after-a-worker-rebuild)). Check the principal and the namespace. |
| `tree_mismatch` | The signed payload names another tree than the ref. | Should not occur. Investigate the ref and the engine. Anyone with push access can copy a valid verdict commit to another ref name. This check catches that replay. |
| `job_mismatch` | The signed payload names another job than the ref. | Should not occur. Investigate the ref and the engine. Anyone with push access can copy a valid verdict commit to another ref name. This check catches that replay. |
| `argv_mismatch` | The signed argv differs from the `argv` input. | Make the input equal the job's `run.argv`. The job must have `args = "none"`. |
| `not_passed` | The payload's outcome is not `passed`. A worker never signs a failure. | Should not occur. Investigate the ref and the engine, and tell the Pandora owner. |
| `kind_mismatch` | The payload is not a `pandora-verdict`. | As `not_passed`. |
| `version_mismatch` | The payload version is not 1. A newer Pandora wrote it. | Bump the action to the release that matches the workers. |
| `malformed_verdict` | The verdict commit lacks `payload.json`, `verdict.sig` or `signer`. | Delete the ref. The next run republishes it. |
| `malformed_payload` | The payload is not a JSON object, or the field check could not run. | As `malformed_verdict`. |
| `bad_job` | The `job` input is not a valid job id. | Fix the `job` input. |
| `bad_argv` | The `argv` input is empty, or is not a JSON array of strings. | Fix the `argv` input. |
| `bad_input` | The script got an unknown or incomplete option. | Fix the step. This should not happen with the action. |
| `no_default_branch` | The default branch name is unknown: the event carries none, and `origin` names no `HEAD`. | Run the step on a `push` or `pull_request` event. |
| `bad_default_branch` | The default branch name is not a valid branch name. | Check the repository settings. |
| `base_unavailable` | The default branch could not be fetched. | Check the runner's access to the repository, then rerun. |
| `no_tree` | The checkout has no `HEAD` commit. | Put `actions/checkout` before the step. |
| `no_ssh_keygen` | The runner has no `ssh-keygen`. | Use a runner image with OpenSSH. |
| `no_python3` | The runner has no `python3`. | Use a runner image with Python 3. |
| `no_tempdir` | `mktemp -d` failed. | Check the runner's disk. |
| `script_error` | The verify script did not run to the end. | Read the step log. Report it to the Pandora owner. |

## Trust rules

* CI reads the allowed signers from the default branch only. It never reads
  them from the pull request's head or from the base a pull request picks. A
  change cannot add the key that vouches for it.
* The verifier files are review-sensitive: the action, the script and the
  workflow step that calls them. CI runs the head's copy of the workflow, so a
  pull request that edits the step can skip the job. Require the owner's review
  for those files, for example with CODEOWNERS.
* Failures never transfer. A failed or missing verdict only means the job runs.
* A verdict means pass only. The worker signs nothing for a failed run.
* A verdict has no TTL. It holds for its tree until someone deletes the ref.
* The tree is `HEAD^{tree}` of the CI checkout. On a pull request that is the
  merge commit GitHub builds, not the branch head.
* CI does not pin the golden fingerprint yet. It records the verdict's golden,
  but does not compare it with an expected value.

## What a verdict does not check

A verdict trusts the worker's ready state, its kernel check and git's view of
the bytes. Package drift after the last canary, a worker under a custom root,
and files git normalizes are listed in
[docs/worker.md, Verdicts](worker.md#verdicts). Trust a signer only as far as
every key that can reach the worker.

## Pruning

Each tree that passes adds one ref per job. Nothing removes them. A pruned
ref costs nothing but a CI run of the job, if that tree is ever checked again.

Pandora ships a reusable workflow that deletes old verdict refs:
[`.github/workflows/verdict-prune.yml`](../.github/workflows/verdict-prune.yml).
It runs [`scripts/verdict-prune.sh`](../scripts/verdict-prune.sh) at the
workflow's own commit. Pandora's own
[`prune.yml`](../.github/workflows/prune.yml) calls it the same way.

### Call it from the repository

Add `.github/workflows/verdict-prune.yml` to the repository:

```yaml
name: verdict-prune

on:
  schedule:
    - cron: '17 6 * * 1'
  workflow_dispatch:
    inputs:
      dry_run:
        description: Print what would be deleted and delete nothing.
        type: boolean
        default: true

permissions:
  contents: read

jobs:
  prune:
    permissions:
      contents: write
    uses: gbasin/pandora/.github/workflows/verdict-prune.yml@<full sha> # vX.Y.Z
    with:
      max_age_days: 30
      dry_run: ${{ github.event_name == 'workflow_dispatch' && inputs.dry_run }}
```

* Use a sha of a Pandora release that contains the workflow, newer than
  v0.3.12.
* Only this job gets `contents: write`. It needs it to delete refs.
* `max_age_days` defaults to 30. It must be at least 1. The workflow and the
  script refuse 0, which would delete every verdict.
* `dry_run: true` prints what would be deleted and deletes nothing.
* The scheduled run deletes. A run started by hand is a dry run unless you
  clear the `dry_run` box.

Do a dry run before the first scheduled run:

1. Merge the workflow.
2. Open the Actions tab, select `verdict-prune`, and click "Run workflow".
   Leave `dry_run` checked.
3. Read the `would delete` lines and the `::notice::` summary.
4. If the list is wrong, change `max_age_days` or remove the schedule before
   Monday.

### How it decides

1. It lists `refs/pandora/verdicts/*` on `origin` with `git ls-remote`.
2. It fetches all of them in one `git fetch` into a private namespace,
   `refs/pandora-prune/*`, and removes that namespace when it exits.
3. It reads every `payload.json` with one `git cat-file --batch`.
4. It deletes a ref when its `finished` time is more than `max_age_days` ago.
5. It sends deletes in batches of 100 with `git push --porcelain`, each ref
   under `--force-with-lease` on the listed value. It reads the result of each
   ref, because a push deletes what it can even when it rejects another ref.

The age is the payload's `finished` field, the epoch seconds the worker signed.
The commit date cannot serve: every verdict commit is dated `@1 +0000`, so the
same verdict always makes the same commit. One fetch for all refs costs less
than one shallow fetch per ref, because each verdict holds three small blobs
and each separate fetch pays its own round trip.

A ref with no readable `finished` time is kept and counted as unreadable. A
ref that changed after the listing is kept and counted as changed. Git
reports it as a stale lease, and that does not fail the step. The step ends
with one `::notice::` line: refs seen, older than the threshold, deleted,
kept, changed and unreadable. A failed listing or fetch fails the step. So
does any other rejected delete, after the rest of the batch is deleted and
counted.

Run the script by hand from a checkout with push access:
`scripts/verdict-prune.sh --dry-run`. Its options are `--remote`,
`--max-age-days`, `--batch` and `--dry-run`.
