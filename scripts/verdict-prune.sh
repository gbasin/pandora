#!/usr/bin/env bash
# Delete Pandora verdict refs older than a threshold from a remote.
#
#   scripts/verdict-prune.sh [--remote origin] [--max-age-days 30]
#       [--batch 100] [--dry-run] [--now <epoch seconds>]
#
# Run from a git checkout whose remote may push. The reusable workflow
# .github/workflows/verdict-prune.yml runs it weekly for a consuming
# repository (docs/verdicts.md, Pruning).
#
# Age comes from the `finished` field of each verdict's payload.json, the
# epoch seconds the worker signed. The commit date cannot serve: every verdict
# commit is dated `@1 +0000` so the same verdict always makes the same commit,
# and a remote keeps no reflog we could read.
#
# The script lists refs/pandora/verdicts/* with `git ls-remote`, fetches them
# all with one `git fetch` into a private namespace (refs/pandora-prune/*),
# and reads every payload with one `git cat-file --batch`. One round trip and
# one process serve every ref; a verdict commit holds three small blobs, so
# fetching all of them costs less than one shallow fetch per ref, which pays a
# round trip and a negotiation each time.
#
# A ref is deleted when `now - finished` exceeds --max-age-days. A ref whose
# payload is missing, unreadable, or has no numeric `finished` is kept and
# counted as unreadable: the script deletes only what it can show is old.
# Deletes go out in batches of --batch refs, each with --force-with-lease on
# the value listed, so a ref that changed after the listing stays and is
# counted as changed. Each ref's result is read from `git push --porcelain`,
# because a push deletes what it can even when it rejects another ref.
#
# --now fixes the clock, for the unit tests. --dry-run prints what it would
# delete and deletes nothing. Exit 0 on success, 1 when listing or fetching
# failed or a delete was rejected for any reason but a stale lease, 2 on a bad
# argument. --max-age-days is at least 1.

set -euo pipefail

remote=origin
max_age_days=30
batch=100
dry_run=false
now=''
scratch=refs/pandora-prune/

die() {
    echo "pandora verdict prune: $2" >&2
    exit "$1"
}

while [ $# -gt 0 ]; do
    case $1 in
        --remote) remote=${2-}; shift 2 || die 2 'bad arguments' ;;
        --max-age-days) max_age_days=${2-}; shift 2 || die 2 'bad arguments' ;;
        --batch) batch=${2-}; shift 2 || die 2 'bad arguments' ;;
        --now) now=${2-}; shift 2 || die 2 'bad arguments' ;;
        --dry-run) dry_run=true; shift ;;
        *) die 2 "unknown argument: $1" ;;
    esac
done

# 0 would delete every verdict, so the floor is 1.
[[ $max_age_days =~ ^[1-9][0-9]*$ ]] || die 2 "--max-age-days must be a whole number of at least 1: $max_age_days"
[[ $batch =~ ^[1-9][0-9]*$ ]] || die 2 "--batch must be a positive whole number: $batch"
if [ -z "$remote" ] || [ "${remote#-}" != "$remote" ]; then die 2 "bad remote: $remote"; fi
[ -n "$now" ] || now=$(date +%s)
[[ $now =~ ^[0-9]+$ ]] || die 2 "--now must be epoch seconds: $now"
command -v python3 >/dev/null 2>&1 || die 1 'python3 not found'
git rev-parse --git-dir >/dev/null 2>&1 || die 1 'not in a git repository'

cleanup() {
    git for-each-ref --format='delete %(refname)' "$scratch" 2>/dev/null \
        | git update-ref --stdin 2>/dev/null || true
}
trap cleanup EXIT

listing=$(git ls-remote --refs "$remote" 'refs/pandora/verdicts/*') \
    || die 1 "git ls-remote $remote failed"
if [ -z "$listing" ]; then
    echo "pandora verdict prune: no verdict refs on $remote"
    exit 0
fi

cleanup
git fetch --quiet --no-tags --no-write-fetch-head "$remote" \
    "+refs/pandora/verdicts/*:${scratch}*" || die 1 "git fetch $remote failed"

# Lines out: "delete <oid> <ref>", "keep <oid> <ref>" or "unreadable <oid> <ref>".
# The listing goes in on stdin: one environment string is capped at 128 KiB on
# Linux, about 1,200 refs.
prog='
import json, math, re, subprocess, sys

now, days = int(sys.argv[1]), int(sys.argv[2])
limit = days * 86400
refs = []
for line in sys.stdin.read().splitlines():
    oid, _, ref = line.partition("\t")
    if re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", oid) and \
            re.fullmatch(r"refs/pandora/verdicts/[0-9a-f]{40,64}/[a-z][a-z0-9-]*", ref):
        refs.append((oid, ref))
query = "".join("%s:payload.json\n" % oid for oid, _ in refs).encode()
out = subprocess.run(["git", "cat-file", "--batch"], input=query,
                     stdout=subprocess.PIPE, check=True).stdout
pos = 0
for oid, ref in refs:
    end = out.index(b"\n", pos)
    header = out[pos:end].split()
    pos = end + 1
    finished = None
    # "<oid> <type> <size>" is followed by <size> bytes and a newline whatever
    # the type, so consume them for every object. Anything but a blob is kept.
    # Other headers ("<name> missing", "<name> ambiguous") carry no body.
    if len(header) == 3:
        size = int(header[2])
        body = out[pos:pos + size]
        if len(body) != size or out[pos + size:pos + size + 1] != b"\n":
            sys.exit("git cat-file --batch output ended early")
        pos += size + 1
        if header[1] == b"blob":
            try:
                finished = json.loads(body.decode("utf-8")).get("finished")
            except (ValueError, AttributeError):
                finished = None
    elif len(header) != 2:
        sys.exit("git cat-file --batch printed an unexpected header")
    if type(finished) not in (int, float) or not math.isfinite(finished):
        print("unreadable", oid, ref)
    elif now - finished > limit:
        print("delete", oid, ref)
    else:
        print("keep", oid, ref)
if pos != len(out):
    sys.exit("git cat-file --batch printed more than was asked")
'
decisions=$(printf '%s\n' "$listing" | python3 -c "$prog" "$now" "$max_age_days") \
    || die 1 'reading the payloads failed'

total=0 kept=0 unreadable=0 old=0 deleted=0 changed=0 failed=0
leases=()
specs=()

# A push is not atomic: git deletes what it can and exits 1 if any ref was
# rejected. So read the result of each ref from --porcelain, never the exit
# code. A stale lease means the ref changed after the listing: it is kept, as
# intended. Any other rejection, or a ref the push never reported, fails.
flush() {
    [ ${#specs[@]} -gt 0 ] || return 0
    local out flag spec summary ref reported=0
    out=$(git push --porcelain --no-verify "${leases[@]}" "$remote" "${specs[@]}" 2>&1) || true
    while IFS=$'\t' read -r flag spec summary; do
        # ":<ref>" for a delete sent, "(delete):<ref>" for one rejected here.
        case $spec in
            :refs/pandora/verdicts/* | '(delete):refs/pandora/verdicts/'*) ref=${spec#*:} ;;
            *) continue ;;
        esac
        reported=$((reported + 1))
        case $flag in
            -) deleted=$((deleted + 1)) ;;
            '!')
                if [ "$summary" = '[rejected] (stale info)' ]; then
                    changed=$((changed + 1))
                    echo "pandora verdict prune: kept $ref: it changed after the listing"
                else
                    failed=1
                    echo "pandora verdict prune: delete $ref failed: $summary" >&2
                fi
                ;;
            *)
                failed=1
                echo "pandora verdict prune: delete $ref: unexpected result $flag $summary" >&2
                ;;
        esac
    done <<<"$out"
    if [ "$reported" -ne ${#specs[@]} ]; then
        failed=1
        echo "pandora verdict prune: git push reported $reported of ${#specs[@]} refs:" >&2
        printf '%s\n' "$out" >&2
    fi
    leases=()
    specs=()
}

while read -r action oid ref; do
    [ -n "${action:-}" ] || continue
    total=$((total + 1))
    case $action in
        keep) kept=$((kept + 1)) ;;
        unreadable)
            unreadable=$((unreadable + 1))
            echo "pandora verdict prune: kept $ref: no readable finished time"
            ;;
        delete)
            old=$((old + 1))
            if [ "$dry_run" = true ]; then
                echo "pandora verdict prune: would delete $ref"
                continue
            fi
            echo "pandora verdict prune: delete $ref"
            leases+=("--force-with-lease=$ref:$oid")
            specs+=(":$ref")
            [ ${#specs[@]} -lt "$batch" ] || flush
            ;;
    esac
done <<<"$decisions"
flush

summary="$total refs, $old older than $max_age_days days, $deleted deleted, $kept kept, $changed changed, $unreadable unreadable"
[ "$dry_run" = false ] || summary="$summary (dry run)"
echo "pandora verdict prune: $summary"
if [ -n "${GITHUB_ACTIONS:-}" ]; then
    echo "::notice::pandora verdict prune: $summary"
fi
exit "$failed"
