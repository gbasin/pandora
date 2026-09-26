import re

FAIL_MARKERS = [
 (re.compile(r"\bTest Files\s+\d+ failed"), "vitest_files_failed"),
 (re.compile(r"\bTests\s+\d+ failed"), "vitest_tests_failed"),
 (re.compile(r"^\s*FAIL\s", re.M), "fail_line"),
 (re.compile(r"\bFAIL\s+\|"), "fail_proj"),
 (re.compile(r"error TS\d{4}"), "tsc_error"),
 (re.compile(r"✖ failing tests"), "nodetest_fail"),
 (re.compile(r"^not ok \d", re.M), "tap_notok"),
 (re.compile(r"ℹ fail [1-9]"), "nodetest_failcount"),
 (re.compile(r"Tasks:\s+\d+ successful, \d+ total[\s\S]{0,80}?Failed:\s*[1-9]"), "turbo_failed"),
 (re.compile(r"\bFailed:\s+[1-9]\d*\b"), "turbo_failed2"),
 (re.compile(r"ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL|ELIFECYCLE"), "pnpm_lifecycle"),
 (re.compile(r"Found \d+ errors?\b"), "tsc_found_errors"),
 (re.compile(r"×\s+\S+.*\n", re.M), "oxlint_x"),
 (re.compile(r"Found \d+ warnings? and [1-9]\d* errors?"), "oxlint_errors"),
 (re.compile(r"^\s*\d+ problems? \([1-9]", re.M), "eslint_problems"),
 (re.compile(r"\b\d+ failed\b"), "generic_failed"),
 (re.compile(r":\s*fail(\s|\()", re.I), "journey_fail"),
 (re.compile(r"\b[1-9]\d* fail\b"), "journey_failcount"),
 (re.compile(r"Command failed with exit code [1-9]"), "cmd_failed"),
 (re.compile(r"\bAssertionError\b|\bexpected .* to (deeply )?equal\b"), "assertion"),
 (re.compile(r"\b(\d+) (?:specs?|tests?) failed", re.I), "pw_failed"),
 (re.compile(r"Error: expect\("), "pw_expect"),
 (re.compile(r"Timed out \d+ms waiting"), "pw_timeout"),
]
PASS_MARKERS = [
 (re.compile(r"\bTest Files\s+\d+ passed"), "vitest_pass"),
 (re.compile(r"\bTests\s+\d+ passed"), "vitest_tests_pass"),
 (re.compile(r"Tasks:\s+\d+ successful, \d+ total"), "turbo_ok"),
 (re.compile(r"All matched files use the correct format"), "fmt_ok"),
 (re.compile(r"Found 0 warnings and 0 errors"), "oxlint_ok"),
 (re.compile(r"ℹ fail 0"), "nodetest_ok"),
 (re.compile(r"\b\d+ passed\b"), "generic_pass"),
 (re.compile(r"0 fail\b"), "zero_fail"),
 (re.compile(r"\bpass\b"), "pass_word"),
]
ENV_MARKERS = [
 (re.compile(r"command not found|: not found\b"), "cmd_not_found"),
 (re.compile(r"Cannot find module|ERR_MODULE_NOT_FOUND|Cannot find package"), "missing_module"),
 (re.compile(r"ECONNREFUSED|connection refused|could not connect to server"), "conn_refused"),
 (re.compile(r"EADDRINUSE|address already in use|port \d+ is (already )?in use", re.I), "port_in_use"),
 (re.compile(r"Cannot connect to the Docker daemon|docker daemon is not running|Is the docker daemon running", re.I), "docker_down"),
 (re.compile(r"browserType\.launch|Executable doesn't exist|playwright install", re.I), "pw_browser"),
 (re.compile(r"ERR_PNPM_OUTDATED_LOCKFILE|ERR_PNPM_NO_MATCHING_VERSION|node_modules.*missing|check-worktree-deps", re.I), "deps"),
 (re.compile(r"ENOSPC|EMFILE|out of memory|JavaScript heap out of memory", re.I), "resources"),
 (re.compile(r"turbo.*daemon|Daemon is not running", re.I), "turbo_daemon"),
 (re.compile(r"role \"?\w+\"? does not exist|database \".*\" does not exist|relation \".*\" does not exist"), "db_missing"),
]

EXIT_RE = re.compile(r"^Exit code (\d+)", re.M)

def outcome(body, err):
    if body is None: body=""
    b = body
    fails=[n for rx,n in FAIL_MARKERS if rx.search(b)]
    passes=[n for rx,n in PASS_MARKERS if rx.search(b)]
    envs=[n for rx,n in ENV_MARKERS if rx.search(b)]
    m=EXIT_RE.search(b)
    exit_code = int(m.group(1)) if m else (1 if err else 0 if body else None)
    strong_fail = [f for f in fails if f not in ("generic_failed","journey_fail","journey_failcount","assertion","oxlint_x")]
    if strong_fail: st="red"
    elif err and fails: st="red"
    elif fails and not passes: st="red"
    elif passes and not fails: st="green"
    elif passes and fails: st="red"
    elif err: st="red"
    elif not b.strip(): st="unknown"
    else: st="unknown"
    return st, fails, passes, envs, exit_code
