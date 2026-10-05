#!/usr/bin/env bash
# Verify a signed Pandora run verdict for the tree checked out here.
#
#   scripts/verdict-verify.sh --job suite \
#       --argv '["python3","-m","unittest","discover","-s","pandora"]' \
#       [--signers .github/pandora/allowed_signers] [--default-branch main]
#
# Run from the root of a git checkout whose `origin` holds the verdict refs.
# The tree is HEAD^{tree}. The verdict is the parentless commit at
# refs/pandora/verdicts/<tree>/<job>, holding payload.json, verdict.sig and
# signer.
#
# The allowed signers file is read from the repository's default branch and
# from nothing else: not the working tree, not HEAD, not the base a pull
# request chose. A change cannot vouch for itself by adding a key, and a pull
# request aimed at another branch cannot pick a signers file that branch holds.
# The default branch is --default-branch (the action passes
# github.event.repository.default_branch), else the branch origin's HEAD names.
# It is fetched at depth 1 into refs/remotes/origin/<branch> and read by that
# full ref, so a tag or branch named `origin/<branch>` cannot stand in for it.
# The same holds on a push event: the signers come from the fetched default
# branch, not from the pushed commit.
#
# --base <rev> reads the signers file from <rev> as given, with no fetch. It
# exists for the unit tests only; CI never passes it.
#
# Prints verified=, reason=, run_id= and golden= lines on stdout, appends them
# to $GITHUB_OUTPUT when it is set, and prints one ::notice:: line. Exit 0
# whatever the answer: a verification problem is verified=false and a reason,
# never a failed step.

set -euo pipefail

job=''
argv=''
signers='.github/pandora/allowed_signers'
base=''
default_branch=''
verified=false
reason=''
run_id=''
golden=''
work=''

finish() {
    reason=$1
    if [ "$verified" = true ]; then
        echo "::notice::pandora verdict: verified run $run_id for $job (golden $golden); $reason"
    else
        echo "::notice::pandora verdict: not verified for ${job:-?}: $reason"
    fi
    {
        echo "verified=$verified"
        echo "reason=$reason"
        echo "run_id=$run_id"
        echo "golden=$golden"
    } | tee -a "${GITHUB_OUTPUT:-/dev/null}"
    if [ -n "$work" ]; then
        rm -rf -- "$work"
    fi
    exit 0
}

while [ $# -gt 0 ]; do
    case $1 in
        --job) job=${2-}; shift 2 || finish bad_input ;;
        --argv) argv=${2-}; shift 2 || finish bad_input ;;
        --signers) signers=${2-}; shift 2 || finish bad_input ;;
        --default-branch) default_branch=${2-}; shift 2 || finish bad_input ;;
        # Tests only: read the signers file from this rev, unfetched.
        --base) base=${2-}; shift 2 || finish bad_input ;;
        *) finish "bad_input" ;;
    esac
done

# The job id is a pandora.toml id; anything else cannot name a ref.
[[ $job =~ ^[a-z][a-z0-9-]*$ ]] || finish bad_job
[ -n "$argv" ] || finish bad_argv
[ -n "$signers" ] || finish no_signers
command -v ssh-keygen >/dev/null 2>&1 || finish no_ssh_keygen
command -v python3 >/dev/null 2>&1 || finish no_python3

tree=$(git rev-parse --verify --quiet 'HEAD^{tree}' 2>/dev/null) || finish no_tree

if [ -z "$base" ]; then
    if [ -z "$default_branch" ]; then
        # "ref: refs/heads/<name>\tHEAD" names the branch origin's HEAD points at.
        default_branch=$(git ls-remote --symref origin HEAD 2>/dev/null \
            | sed -n 's|^ref: refs/heads/\([^[:space:]]*\)[[:space:]]*HEAD$|\1|p') || true
    fi
    [ -n "$default_branch" ] || finish no_default_branch
    git check-ref-format --branch "$default_branch" >/dev/null 2>&1 \
        || finish bad_default_branch
    git fetch --quiet --depth 1 origin \
        "+refs/heads/$default_branch:refs/remotes/origin/$default_branch" \
        >/dev/null 2>&1 || finish base_unavailable
    base="refs/remotes/origin/$default_branch"
fi

# A missing ref, an unreachable origin, a shallow-fetch refusal: all a miss.
git fetch --quiet --depth 1 origin "refs/pandora/verdicts/$tree/$job" \
    >/dev/null 2>&1 || finish no_verdict
commit=$(git rev-parse --verify --quiet 'FETCH_HEAD^{commit}' 2>/dev/null) \
    || finish no_verdict

work=$(mktemp -d 2>/dev/null) || finish no_tempdir
git show "$commit:payload.json" >"$work/payload.json" 2>/dev/null || finish malformed_verdict
git show "$commit:verdict.sig" >"$work/verdict.sig" 2>/dev/null || finish malformed_verdict
git show "$commit:signer" >"$work/signer" 2>/dev/null || finish malformed_verdict

git show "$base:$signers" >"$work/allowed_signers" 2>/dev/null || finish signers_missing
grep -Eq '^[[:space:]]*[^#[:space:]]' "$work/allowed_signers" || finish no_signers

ssh-keygen -Y verify -f "$work/allowed_signers" -I pandora-verdict -n pandora-verdict \
    -s "$work/verdict.sig" <"$work/payload.json" >/dev/null 2>&1 || finish bad_signature

# One line out: "<reason> <run_id> <golden>", reason `match` when every field
# agrees. Fields that are not plain identifiers are left empty.
checked=$(python3 - "$work/payload.json" "$tree" "$job" "$argv" <<'PY' 2>/dev/null
import json, re, sys
path, tree, job, argv = sys.argv[1:5]
try:
    want = json.loads(argv)
    if not isinstance(want, list) or not all(isinstance(x, str) for x in want):
        raise ValueError
except ValueError:
    print('bad_argv'); sys.exit()
try:
    payload = json.loads(open(path, 'rb').read().decode('utf-8'))
    if not isinstance(payload, dict):
        raise ValueError
except ValueError:
    print('malformed_payload'); sys.exit()
def plain(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._-]{1,128}', value) else ''
# Exact types: JSON true and 1.0 both compare equal to 1 in Python.
kind, version = payload.get('kind'), payload.get('v')
checks = (('kind_mismatch', type(kind) is str and kind == 'pandora-verdict'),
          ('version_mismatch', type(version) is int and version == 1),
          ('tree_mismatch', payload.get('tree') == tree),
          ('job_mismatch', payload.get('job') == job),
          ('argv_mismatch', payload.get('argv') == want),
          ('not_passed', payload.get('outcome') == 'passed'))
reason = next((name for name, ok in checks if not ok), 'match')
print(reason, plain(payload.get('run_id')) or '-', plain(payload.get('golden')) or '-')
PY
) || finish malformed_payload
read -r result found_run found_golden <<<"$checked" || true
[ "${found_run:--}" = - ] || run_id=$found_run
[ "${found_golden:--}" = - ] || golden=$found_golden
[ "${result:-}" = match ] || finish "${result:-malformed_payload}"
verified=true
finish match
