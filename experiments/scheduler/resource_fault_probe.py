#!/usr/bin/env python3
"""Run bounded Docker resource-fault checks through the integrated warm workflow.

This is an operator-run probe.  It never starts Docker locally: every build, run,
and logical image removal is one ``warm.py --workflow docker`` admission.  Its
only persistent local evidence is the requested output directory.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


BASE_IMAGE = "node:24-bookworm-slim"
WORKFLOW_GUARD_SECONDS = 1500
# This bounds only the local client.  On expiry we deliberately do not kill or
# retry it: the remote request may already be admitted and must be recovered by
# its immutable attempt identity.
SUBPROCESS_DEADLINE_SECONDS = 1800


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def git(*argv, cwd=None):
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True, timeout=30)


def terminal(attempt):
    path = attempt / "terminal.json"
    return json.loads(path.read_text()) if path.is_file() else None


def docker_report(attempt):
    path = attempt / "results" / "docker.json"
    return json.loads(path.read_text()) if path.is_file() else None


def container_state(attempt):
    path = attempt / "results" / "container-state.json"
    return json.loads(path.read_text()) if path.is_file() else None


def run_warm(warm, host, repo, output, name, spec, attempts):
    """Submit exactly one immutable warm request and save its complete client log."""
    identity = uuid.uuid4().hex
    attempt = output / "runs" / name
    log_path = output / "logs" / (name + ".log")
    spec_path = attempt.with_suffix(".external-spec.json")
    attempt.parent.mkdir(exist_ok=True)
    output.joinpath("logs").mkdir(exist_ok=True)
    write_json(spec_path, spec)
    argv = [
        sys.executable,
        "-B",
        str(warm),
        "--host",
        host,
        "--repo",
        str(repo),
        "--output",
        str(attempt),
        "--attempt",
        identity,
        "--workflow",
        "docker",
        "--queue-timeout-seconds",
        "180",
        "--docker-request",
        json.dumps(spec, separators=(",", ":")),
    ]
    started = time.monotonic()
    with log_path.open("w") as log:
        process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
        try:
            status = process.wait(timeout=SUBPROCESS_DEADLINE_SECONDS)
            timed_out = False
        except subprocess.TimeoutExpired:
            # Do not terminate this process.  Killing a client cannot prove the
            # remote worker did not accept the attempt, and a retry would create
            # an unrelated request.
            status = None
            timed_out = True
    log_text = log_path.read_text(errors="replace")
    record = {
        "name": name,
        "attempt": identity,
        "argv": argv,
        "external_spec": str(spec_path.relative_to(output)),
        "log": str(log_path.relative_to(output)),
        "guard_seconds": WORKFLOW_GUARD_SECONDS,
        "subprocess_deadline_seconds": SUBPROCESS_DEADLINE_SECONDS,
        "seconds": round(time.monotonic() - started, 2),
        "exit_code": status,
        "local_client_timed_out": timed_out,
        "terminal": terminal(attempt),
        "docker": docker_report(attempt),
        "container_state": container_state(attempt),
    }
    attempts.append(record)
    return record, log_text


def assert_clean(record):
    assert record["terminal"], f"{record['name']}: missing terminal receipt"
    assert record["terminal"].get("cleanup_verified") is True, f"{record['name']}: cleanup was not verified"


def build_spec(key, tag, dockerfile="Dockerfile"):
    return {
        "worktree_key": key,
        "request": {"kind": "build", "tag": tag, "dockerfile": dockerfile},
        "config": {"outputs": [], "network": "none", "queue_timeout_seconds": 180},
    }


def run_spec(key, tag, command):
    return {
        "worktree_key": key,
        "request": {"kind": "run", "tag": tag, "mount": None, "command": command},
        "config": {"outputs": [], "network": "none", "queue_timeout_seconds": 180},
    }


def remove_spec(key, tag):
    return {
        "worktree_key": key,
        "request": {"kind": "remove", "tag": tag},
        "config": {"outputs": [], "network": "none", "queue_timeout_seconds": 180},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="SSH destination accepted by warm.py")
    parser.add_argument("--output", required=True, type=Path, help="New evidence directory")
    parser.add_argument(
        "--warm-script",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "warm" / "warm.py",
        help="Integrated warm.py from this checkout",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    warm = args.warm_script.resolve()
    if not warm.is_file():
        parser.error("--warm-script does not name a file")
    output.mkdir(parents=True, exist_ok=False)
    (output / "runs").mkdir()
    attempts = []
    result = {
        "base_image": BASE_IMAGE,
        "workflow_guard_seconds": WORKFLOW_GUARD_SECONDS,
        "subprocess_deadline_seconds": SUBPROCESS_DEADLINE_SECONDS,
        "attempts": attempts,
        "assertions": {},
        "scope": {
            "memory": "cgroup-bounded allocations only, at most 12 GiB requested",
            "filesystem": "a bounded /dev/shm ENOSPC check, not VM-disk exhaustion",
        },
    }
    tag = "pandora-resource-fault:" + uuid.uuid4().hex
    mapping_created = False
    ambiguous = False
    try:
        with tempfile.TemporaryDirectory(prefix="resource-fault-fixture-", dir=output) as temporary:
            temporary = Path(temporary)
            seed = temporary / "seed"
            worktree = temporary / "worktree"
            seed.mkdir()
            (seed / "Dockerfile").write_text(
                "FROM " + BASE_IMAGE + "\n"
                "CMD [\"node\", \"-e\", \"process.stdout.write('RESOURCE_FAULT_SENTINEL\\\\n')\"]\n"
            )
            (seed / ".dockerignore").write_text(".git\n")
            git("init", "-q", "-b", "resource-fault-seed", str(seed))
            git("-C", str(seed), "add", "Dockerfile", ".dockerignore")
            git("-C", str(seed), "-c", "user.name=Resource Probe", "-c", "user.email=probe@example.invalid",
                "commit", "-qm", "resource fault fixture")
            git("-C", str(seed), "worktree", "add", "-q", "-b", "resource-fault-worktree", str(worktree))
            key = hashlib.sha256(str(worktree.resolve()).encode()).hexdigest()
            result["worktree_key"] = key

            baseline, baseline_log = run_warm(warm, args.host, worktree, output, "baseline-build", build_spec(key, tag), attempts)
            assert not baseline["local_client_timed_out"], "baseline build client deadline reached; remote state retained"
            assert baseline["exit_code"] == 0, "baseline build failed"
            assert_clean(baseline)
            mapping_created = True
            result["assertions"]["baseline_build"] = True

            sentinel, sentinel_log = run_warm(
                warm, args.host, worktree, output, "baseline-sentinel", run_spec(key, tag, []), attempts
            )
            assert not sentinel["local_client_timed_out"], "sentinel client deadline reached; remote state retained"
            assert sentinel["exit_code"] == 0 and "RESOURCE_FAULT_SENTINEL" in sentinel_log, "baseline image did not run"
            assert_clean(sentinel)
            result["assertions"]["baseline_sentinel"] = True

            allocator = (
                "const chunks=[];const chunk=64*1024*1024;const maximum=12*1024*1024*1024;"
                "for(let used=0;used<maximum;used+=chunk){const b=Buffer.alloc(chunk);"
                "for(let page=0;page<b.length;page+=4096)b[page]=1;chunks.push(b);"
                "console.log('TOUCHED_MIB='+(used+chunk)/1024/1024)}"
            )
            oom, oom_log = run_warm(
                warm, args.host, worktree, output, "runtime-cgroup-oom", run_spec(key, tag, ["node", "-e", allocator]), attempts
            )
            assert not oom["local_client_timed_out"], "OOM run client deadline reached; remote state retained"
            assert_clean(oom)
            oom_state = (
                oom["terminal"].get("exit_code") == 137
                or oom["container_state"] and oom["container_state"].get("OOMKilled") is True
                or "container exceeded its memory limit" in oom_log
            )
            assert oom_state, "runtime allocation did not report cgroup OOM (expected exit 137 or OOM evidence)"
            result["assertions"]["runtime_cgroup_oom"] = True

            faulty = (
                "FROM " + BASE_IMAGE + "\nRUN node -e \"" + allocator.replace('"', '\\\"') + "\"\n"
            )
            (worktree / "Dockerfile").write_text(faulty)
            failed_build, failed_build_log = run_warm(
                warm, args.host, worktree, output, "builder-cgroup-oom", build_spec(key, tag), attempts
            )
            assert not failed_build["local_client_timed_out"], "builder OOM client deadline reached; remote state retained"
            assert failed_build["exit_code"] not in (None, 0), "faulty build unexpectedly succeeded"
            assert_clean(failed_build)
            assert any(token in failed_build_log.lower() for token in ("exit code: 137", "signal: killed", "out of memory", "cannot allocate memory")), (
                "failed build did not provide OOM evidence"
            )
            result["assertions"]["builder_failure_cleanup"] = True

            preserved, preserved_log = run_warm(
                warm, args.host, worktree, output, "previous-tag-sentinel", run_spec(key, tag, []), attempts
            )
            assert not preserved["local_client_timed_out"], "prior-tag sentinel client deadline reached; remote state retained"
            assert preserved["exit_code"] == 0 and "RESOURCE_FAULT_SENTINEL" in preserved_log, "failed build replaced prior tag mapping"
            assert_clean(preserved)
            result["assertions"]["failed_build_preserves_mapping"] = True

            # execute_run deliberately sets --shm-size=1g.  1,100 MiB reaches
            # ENOSPC inside that tmpfs while staying well below the 4 GiB main
            # cgroup limit and never allocating the VM's disk.
            disk, disk_log = run_warm(
                warm,
                args.host,
                worktree,
                output,
                "tmpfs-enospc",
                run_spec(key, tag, ["sh", "-c", "dd if=/dev/zero of=/dev/shm/pandora-disk-probe bs=1M count=1100"]),
                attempts,
            )
            assert not disk["local_client_timed_out"], "ENOSPC run client deadline reached; remote state retained"
            assert disk["exit_code"] not in (None, 0), "bounded tmpfs write unexpectedly succeeded"
            assert_clean(disk)
            assert "No space left on device" in disk_log, "tmpfs failure did not report ENOSPC"
            result["assertions"]["bounded_tmpfs_enospc"] = True
    except (AssertionError, OSError, subprocess.SubprocessError, ValueError) as error:
        result["failure"] = str(error)
        ambiguous = any(row["local_client_timed_out"] for row in attempts)
    finally:
        if mapping_created and not ambiguous:
            # Remove the one private logical mapping through the same admission
            # path.  Never inspect or prune shared physical images here.
            try:
                removed, _ = run_warm(warm, args.host, output, output,
                                      "remove-private-mapping", remove_spec(result.get("worktree_key", ""), tag), attempts)
                assert removed["exit_code"] == 0 and not removed["local_client_timed_out"]
                assert_clean(removed)
                result["assertions"]["private_mapping_removed"] = True
            except (AssertionError, OSError, subprocess.SubprocessError) as error:
                result["cleanup_failure"] = str(error)
        elif mapping_created:
            result["mapping_retained"] = "local client deadline left remote state ambiguous; no removal or retry was attempted"
        write_json(output / "result.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not result.get("failure") and not result.get("cleanup_failure") else 1


if __name__ == "__main__":
    raise SystemExit(main())
