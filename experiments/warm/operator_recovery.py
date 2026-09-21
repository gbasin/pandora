#!/usr/bin/env python3
"""One-shot, operator-only recovery for a Pandora worker ledger.

This tool never starts or retries validation.  Its only writes are exact cleanup,
an immutable missing-result acknowledgement, and a drained-ledger migration.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time

from admission import alive, receipt
from resource_ownership import OwnershipUnresolved, check as check_ownership, inventory, labels
from resource_admission import initialize_ledger
from scheduling_policy import validate_config as validate_scheduler_config

try:
    from worker_config import validate as validate_worker_config
except ModuleNotFoundError:
    def validate_worker_config(value):
        raise RecoveryBlocked("Worker configuration validator is unavailable; install the configured worker bundle first")


IDENTITY = re.compile(r"[0-9a-f]{32}\Z")
PENDING = ("service-cleanup.pending", "surface-cleanup.pending", "docker-cleanup.pending", "dependency-cleanup.pending", "suite-cleanup.pending")


class RecoveryBlocked(RuntimeError):
    pass


def _attempt(root, identity):
    if not IDENTITY.fullmatch(identity):
        raise ValueError("Invalid attempt identity")
    path = Path(root) / "runs" / identity
    if not path.is_dir() or path.is_symlink():
        raise RecoveryBlocked("Attempt directory is missing or unsafe")
    return path


def _readonly(path):
    if not path.is_file() or path.is_symlink():
        return None
    return sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)


def _ledger(root):
    database = Path(root) / "resources.sqlite3"
    connection = _readonly(database)
    if connection is None:
        return {"present": False, "tables": [], "metadata": {}, "requests": [], "invocations": []}
    try:
        return _ledger_from_connection(connection)
    finally:
        connection.close()


def _ledger_from_connection(connection):
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    result = {"present": True, "tables": tables, "metadata": {}, "requests": [], "invocations": []}
    if "metadata" in tables:
        result["metadata"] = dict(connection.execute("SELECT key, value FROM metadata"))
    for name in ("requests", "invocations"):
        if name in tables:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(" + name + ")")]
            result[name] = [dict(zip(columns, row)) for row in connection.execute("SELECT * FROM " + name + " ORDER BY 1")]
    return result


def _safe_receipt(path, identity):
    try:
        return path.is_file() and not path.is_symlink() and receipt(path, identity)
    except (OSError, ValueError, TypeError, AttributeError): return False


def _terminal_evidence(path, identity):
    try:
        if not _safe_receipt(path, identity): return False
        from evidence import validate_evidence
        attempt = path.parent
        validate_evidence(attempt, identity, json.loads((attempt / "submission.json").read_text()))
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _operator_result(path, identity):
    if not path.is_file() or path.is_symlink():
        return False
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(value, dict): return False
    allowed = {"attempt", "state", "reason", "cleanup_verified", "acknowledged_at", "submission_sha256", "source_digest", "workflow"}
    if "terminal_sha256" in value: allowed.add("terminal_sha256")
    return (set(value) == allowed
            and value["attempt"] == identity and value["state"] == "infrastructure-failed"
            and value["cleanup_verified"] is True and isinstance(value["reason"], str) and value["reason"]
            and isinstance(value["acknowledged_at"], (int, float)) and not isinstance(value["acknowledged_at"], bool)
            and ("terminal_sha256" not in value or isinstance(value["terminal_sha256"], str) and value["terminal_sha256"])
            and all(isinstance(value[key], str) and value[key] for key in ("submission_sha256", "source_digest", "workflow")))


def _binding(attempt):
    submission = attempt / "submission.json"
    if not submission.is_file() or submission.is_symlink():
        raise RecoveryBlocked("Attempt submission is unavailable for acknowledgement")
    content = submission.read_bytes()
    try:
        value = json.loads(content)
    except ValueError as error:
        raise RecoveryBlocked("Attempt submission is malformed") from error
    if not isinstance(value, dict) or value.get("attempt") != attempt.name:
        raise RecoveryBlocked("Attempt submission has no immutable attempt identity")
    if not isinstance(value.get("source_digest"), str) or not value["source_digest"] or not isinstance(value.get("workflow"), str) or not value["workflow"]:
        raise RecoveryBlocked("Attempt submission has no immutable identity")
    result = {"submission_sha256": hashlib.sha256(content).hexdigest(),
              "source_digest": value["source_digest"], "workflow": value.get("workflow", "surface")}
    terminal = attempt / "terminal.json"
    if terminal.exists():
        if not terminal.is_file() or terminal.is_symlink():
            raise RecoveryBlocked("Terminal evidence is unsafe")
        result["terminal_sha256"] = hashlib.sha256(terminal.read_bytes()).hexdigest()
    return result


def _acknowledgement_matches(attempt, identity):
    """Return true only for the immutable receipt bound to current evidence."""
    target = attempt / "operator-result.json"
    if not _operator_result(target, identity):
        return False
    try:
        value = json.loads(target.read_text())
        binding = _binding(attempt)
    except (OSError, ValueError, TypeError, AttributeError, RecoveryBlocked):
        return False
    required = set(binding) | {"attempt", "state", "reason", "cleanup_verified", "acknowledged_at"}
    return set(value) == required and all(value.get(key) == expected for key, expected in binding.items())


def _state(root, identity):
    path = _attempt(root, identity)
    terminal = _terminal_evidence(path / "terminal.json", identity)
    cleaned = _safe_receipt(path / "admission-cleanup.json", identity)
    acknowledged = _operator_result(path / "operator-result.json", identity)
    return {"attempt": identity, "alive": alive(path), "terminal_verified": terminal,
            "cleanup_verified": cleaned, "acknowledged": acknowledged,
            "pending": [name for name in PENDING if (path / name).exists()]}


def inspect(root, *, ownership=check_ownership):
    """Read-only ledger and ownership inspection.  It never constructs Scheduler."""
    root = Path(root)
    ledger = _ledger(root)
    identities = {row.get("attempt") for row in ledger["requests"] if isinstance(row.get("attempt"), str)}
    states = [_state(root, identity) for identity in sorted(identities) if (root / "runs" / identity).is_dir()]
    running = {row.get("attempt") for row in ledger["requests"] if row.get("phase") == "running"}
    admitted = {state["attempt"] for state in states if state["alive"] and state["attempt"] in running}
    try:
        ownership(root, admitted)
        ownership_result = {"resolved": True}
    except (OwnershipUnresolved, OSError, ValueError, subprocess.SubprocessError) as error:
        ownership_result = {"resolved": False, "detail": str(error)}
    configured = root / "worker-config.json"
    current = {"boot_id": None, "config_digest": None, "config_valid": False}
    try:
        current["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        pass
    if configured.is_file() and not configured.is_symlink():
        content = configured.read_bytes()
        try:
            config = validate_worker_config(json.loads(content))
            current["config_digest"] = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            current["config_valid"] = True
        except (ValueError, TypeError, json.JSONDecodeError, RecoveryBlocked):
            pass
    return {"root": str(root), "ledger": ledger, "current": current,
            "attempts": states, "ownership": ownership_result}


def _exact_cleanup_inventory(attempt, *, list_resources=inventory):
    """Validate only resources selected by this exact attempt identity."""
    identity = attempt.name
    names = {"pandora-warm-" + identity + suffix for suffix in ("", "-db", "-pool", "-proxy")}
    network_name = "pandora-warm-" + identity
    try:
        submitted = json.loads((attempt / "submission.json").read_text())
    except (OSError, ValueError) as error:
        raise RecoveryBlocked("Attempt submission is unavailable for exact cleanup") from error
    if not isinstance(submitted, dict):
        raise RecoveryBlocked("Attempt submission is malformed for exact cleanup")
    workflow = submitted.get("workflow", "surface")
    expected = "journey" if workflow in {"journey", "suite"} else workflow
    selected = []
    for item in list_resources(["ps", "-a"]):
        name, raw = item.get("Names"), item.get("Labels", "")
        if not isinstance(name, str) or not isinstance(raw, str):
            raise RecoveryBlocked("Docker inventory has malformed container data")
        if name not in names and ("pandora.attempt=" + identity) not in raw:
            continue
        try:
            metadata = labels(raw)
        except OwnershipUnresolved as error:
            raise RecoveryBlocked("Malformed managed container labels: " + name) from error
        if (name not in names or metadata.get("pandora.attempt") != identity or metadata.get("pandora.workflow") != expected
                or any(key.startswith("pandora.") and key not in {"pandora.attempt", "pandora.workflow", "pandora.experiment"}
                       for key in metadata)):
            raise RecoveryBlocked("Foreign or unlabelled managed container blocks exact cleanup: " + name)
        if expected == "surface" and metadata.get("pandora.experiment") != "warm-surface":
            raise RecoveryBlocked("Surface cleanup labels are incomplete: " + name)
        selected.append(name)
    for item in list_resources(["network", "ls"]):
        name, raw = item.get("Name"), item.get("Labels", "")
        if not isinstance(name, str) or not isinstance(raw, str):
            raise RecoveryBlocked("Docker inventory has malformed network data")
        if name != network_name and ("pandora.attempt=" + identity) not in raw:
            continue
        try:
            metadata = labels(raw)
        except OwnershipUnresolved as error:
            raise RecoveryBlocked("Malformed managed network labels: " + name) from error
        if name != network_name or metadata != {"pandora.attempt": identity}:
            raise RecoveryBlocked("Foreign or unlabelled managed network blocks exact cleanup: " + name)
        selected.append(name)
    return selected


def _reserved_child_is_clear(root, identity, *, list_resources=inventory):
    """A registry reservation is safe only when no exact name or label exists."""
    names = {"pandora-warm-" + identity + suffix for suffix in ("", "-db", "-pool", "-proxy")}
    for command, name_key in ((["ps", "-a"], "Names"), (["network", "ls"], "Name")):
        for item in list_resources(command):
            name, raw = item.get(name_key), item.get("Labels", "")
            if not isinstance(name, str) or not isinstance(raw, str):
                raise RecoveryBlocked("Docker inventory has malformed reserved-child data")
            if name in names or ("pandora.attempt=" + identity) in raw:
                raise RecoveryBlocked("Reserved suite child has an owned resource: " + identity)
    return True


def _cleanup_locked(root, identity, *, run, list_resources, entrypoint=None):
    attempt = _attempt(root, identity)
    if alive(attempt):
        raise RecoveryBlocked("Attempt owner is live; refusing cleanup")
    _exact_cleanup_inventory(attempt, list_resources=list_resources)
    program = entrypoint or attempt / "service_cleanup.py"
    if not program.is_file() or program.is_symlink():
        raise RecoveryBlocked("Attempt has no safe worker-bundled cleanup entrypoint")
    result = run([sys.executable, str(program), str(attempt)], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RecoveryBlocked("Exact cleanup did not verify; resources remain a barrier: " + result.stderr.strip())
    state = _state(root, identity)
    if not state["cleanup_verified"] or state["pending"]:
        raise RecoveryBlocked("Cleanup returned without a verified complete receipt")
    return state


def cleanup(root, identity, *, run=subprocess.run, list_resources=inventory):
    """Invoke the copied, exact-attempt systemd cleanup entrypoint after owner death."""
    attempt = _attempt(root, identity)
    lock = Path(root) / "worker.lock"
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RecoveryBlocked("Worker is busy; wait for its lease before cleanup") from error
        return _cleanup_locked(root, identity, run=run, list_resources=list_resources)


def acknowledge_missing_result(root, identity, reason, *, now=time.time, list_resources=inventory):
    """Record infrastructure failure after cleanup, without synthesizing terminal output."""
    if not reason or not isinstance(reason, str):
        raise ValueError("A nonempty infrastructure reason is required")
    attempt = _attempt(root, identity)
    with (Path(root) / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RecoveryBlocked("Worker is busy; wait for its lease before acknowledgement") from error
        return _acknowledge_locked(root, identity, reason, now=now, list_resources=list_resources)


def _acknowledge_locked(root, identity, reason, *, now, list_resources):
    attempt = _attempt(root, identity)
    state = _state(root, identity)
    target = attempt / "operator-result.json"
    if state["alive"] or state["terminal_verified"]:
        raise RecoveryBlocked("Missing-result acknowledgement requires a dead owner and refuses a valid terminal result")
    if not state["cleanup_verified"] or state["pending"]:
        raise RecoveryBlocked("Missing-result acknowledgement requires verified cleanup")
    if _exact_cleanup_inventory(attempt, list_resources=list_resources):
        raise RecoveryBlocked("Exact owned resources remain after cleanup")
    binding = _binding(attempt)
    if target.exists() or target.is_symlink():
        if _acknowledgement_matches(attempt, identity):
            return json.loads(target.read_text())
        raise RecoveryBlocked("Operator result is immutable and already exists")
    value = {"attempt": identity, "state": "infrastructure-failed", "reason": reason,
             "cleanup_verified": True, "acknowledged_at": now()} | binding
    temporary_path = attempt / ("operator-result." + secrets.token_hex(8) + ".tmp")
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as temporary:
        temporary.write(json.dumps(value, sort_keys=True) + "\n")
        temporary.flush(); os.fsync(temporary.fileno())
    os.replace(temporary_path, target)
    return value


def acknowledge_suite_parent(root, identity, reason, *, now=time.time, run=subprocess.run, list_resources=inventory):
    """Resolve a lost suite only after each staged child is independently resolved."""
    if not reason or not isinstance(reason, str):
        raise ValueError("A nonempty infrastructure reason is required")
    root = Path(root)
    parent = _attempt(root, identity)
    registry = parent / "children.json"
    with (root / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RecoveryBlocked("Worker is busy; wait before suite acknowledgement") from error
        if alive(parent):
            raise RecoveryBlocked("Live suite parent prevents acknowledgement")
        try:
            from suite_parent_cleanup import validate_registry
            children = validate_registry(parent, json.loads(registry.read_text()))
            parent_submission = json.loads((parent / "submission.json").read_text())
        except (OSError, ValueError, TypeError, AttributeError) as error:
            raise RecoveryBlocked("Suite parent registry or submission is malformed") from error
        if not isinstance(parent_submission, dict) or parent_submission.get("attempt") != identity or parent_submission.get("workflow") not in ("suite-run", "surface-run"):
            raise RecoveryBlocked("Parent submission is not a suite run")
        staged = []
        for child in children:
            candidate = root / "runs" / child
            if not candidate.exists():
                _reserved_child_is_clear(root, child, list_resources=list_resources)
                continue
            attempt = _attempt(root, child)
            staged.append(child)
            try: submitted = json.loads((attempt / "submission.json").read_text())
            except (OSError, ValueError, TypeError, AttributeError) as error: raise RecoveryBlocked("Child submission is malformed") from error
            if not isinstance(submitted, dict) or submitted.get("attempt") != child or submitted.get("parent_attempt") != identity:
                raise RecoveryBlocked("Child submission does not belong to this suite parent")
            state = _state(root, child)
            if state["alive"]: raise RecoveryBlocked("Live suite child prevents parent acknowledgement")
            if state["terminal_verified"]:
                if state["pending"] or _exact_cleanup_inventory(attempt, list_resources=list_resources):
                    raise RecoveryBlocked("Verified suite child still has cleanup barriers")
            elif _operator_result(attempt / "operator-result.json", child):
                _acknowledge_locked(root, child, reason, now=now, list_resources=list_resources)
            else:
                _cleanup_locked(root, child, run=run, list_resources=list_resources)
                _acknowledge_locked(root, child, reason, now=now, list_resources=list_resources)
        if alive(parent):
            raise RecoveryBlocked("Live suite parent prevents acknowledgement")
        for child in staged:
            attempt, state = _attempt(root, child), _state(root, child)
            if (state["alive"] or state["pending"] or _exact_cleanup_inventory(attempt, list_resources=list_resources)
                    or not (state["terminal_verified"] or _acknowledgement_matches(attempt, child))):
                raise RecoveryBlocked("Every suite child requires resolved evidence and no cleanup barrier")
        # Use the current operator bundle. Old copied attempt helpers predate
        # operator receipts and must remain immutable evidence.
        _cleanup_locked(root, identity, run=run, list_resources=list_resources,
                        entrypoint=Path(__file__).with_name("service_cleanup.py"))
        for child in staged:
            attempt, state = _attempt(root, child), _state(root, child)
            if (state["alive"] or state["pending"] or _exact_cleanup_inventory(attempt, list_resources=list_resources)
                    or not (state["terminal_verified"] or _acknowledgement_matches(attempt, child))):
                raise RecoveryBlocked("Suite child cleanup changed before parent acknowledgement")
        receipt_path = parent / "operator-cleanup.json"
        stable = {"parent_attempt": identity, "children": children, "cleanup_verified": True}
        if receipt_path.exists() or receipt_path.is_symlink():
            try: existing = json.loads(receipt_path.read_text())
            except (OSError, ValueError, TypeError, AttributeError): existing = None
            if (not receipt_path.is_file() or receipt_path.is_symlink() or not isinstance(existing, dict)
                    or set(existing) != set(stable) | {"acknowledged_at"}
                    or not isinstance(existing.get("acknowledged_at"), (int, float)) or isinstance(existing.get("acknowledged_at"), bool)
                    or any(existing.get(key) != value for key, value in stable.items())):
                raise RecoveryBlocked("Suite operator cleanup receipt is immutable")
        else:
            value = stable | {"acknowledged_at": now()}
            temporary = parent / ("operator-cleanup." + secrets.token_hex(8) + ".tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as target:
                target.write(json.dumps(value, sort_keys=True) + "\n")
                target.flush(); os.fsync(target.fileno())
            os.replace(temporary, receipt_path)
        return _acknowledge_locked(root, identity, reason, now=now, list_resources=list_resources)


def _scheduler_config(config):
    return validate_scheduler_config(validate_worker_config(config)["scheduler"])


def _config(path):
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise RecoveryBlocked("New worker configuration is missing or unsafe")
    value = json.loads(source.read_text())
    validate_worker_config(value)
    return value


def _drained(root, ledger, ownership):
    if not ownership["resolved"]:
        raise RecoveryBlocked("Managed resource ownership is unresolved: " + ownership["detail"])
    runs = Path(root) / "runs"
    if runs.exists():
        if not runs.is_dir() or runs.is_symlink():
            raise RecoveryBlocked("Run evidence directory is unsafe")
        for attempt in runs.iterdir():
            if attempt.is_symlink() or not attempt.is_dir() or not IDENTITY.fullmatch(attempt.name):
                raise RecoveryBlocked("Run evidence contains an unsafe attempt directory")
            state = _state(root, attempt.name)
            if state["alive"]:
                raise RecoveryBlocked("Live attempt " + attempt.name + " prevents migration")
            if state["pending"]:
                raise RecoveryBlocked("Pending cleanup for " + attempt.name + " prevents migration")
    for row in ledger["requests"]:
        identity, phase = row.get("attempt"), row.get("phase")
        if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
            raise RecoveryBlocked("Ledger contains an invalid attempt identity")
        attempt = root / "runs" / identity
        if not attempt.is_dir() or attempt.is_symlink():
            if phase == "running":
                raise RecoveryBlocked("Running attempt " + identity + " has no retained evidence")
            continue
        state = _state(root, identity)
        if state["alive"]:
            raise RecoveryBlocked("Live attempt " + identity + " prevents migration")
        if state["pending"]:
            raise RecoveryBlocked("Pending cleanup for " + identity + " prevents migration")
        if phase == "running" and not state["terminal_verified"]:
            if not (state["cleanup_verified"] and _acknowledgement_matches(attempt, identity)):
                raise RecoveryBlocked("Running attempt " + identity + " lacks a terminal or acknowledged cleanup")
        if phase == "waiting" and state["alive"]:
            raise RecoveryBlocked("Live waiting attempt " + identity + " prevents migration")


def _backup(connection, destination):
    source_path = next(row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main")
    source = sqlite3.connect("file:" + source_path + "?mode=ro", uri=True)
    archived = sqlite3.connect(destination)
    try:
        source.backup(archived)
    finally:
        archived.close()
        source.close()


def _write_generation(root, value):
    temporary = Path(root) / ('ledger-generation.' + secrets.token_hex(8) + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as target:
        target.write(value + '\n')
        target.flush(); os.fsync(target.fileno())
    os.replace(temporary, Path(root) / 'ledger-generation')


def migrate(root, config_path, *, boot_id=None, replace=os.replace, backup=_backup,
            ownership=check_ownership):
    """Archive a drained ledger, then reset its schema in one transaction."""
    root = Path(root)
    config = _config(config_path)
    boot = boot_id or Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not boot:
        raise RecoveryBlocked("New worker boot identity is required")
    lock_path = root / "worker.lock"
    root.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RecoveryBlocked("Worker is busy; wait for its lease before migration") from error
        database = root / "resources.sqlite3"
        had_database = database.exists()
        stamp = str(time.time_ns())
        archive = root / "operator-archive" / stamp
        archive.mkdir(parents=True)
        if had_database and (database.is_symlink() or not database.is_file()):
            raise RecoveryBlocked("Ledger path is unsafe")
        generation = secrets.token_hex(32)
        destination = root / "worker-config.json"
        temporary = root / "worker-config.json.tmp"
        temporary.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
        connection = sqlite3.connect(database, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            ledger = _ledger_from_connection(connection)
            try:
                ownership(root, set())
                ownership_result = {"resolved": True}
            except (OwnershipUnresolved, OSError, ValueError, subprocess.SubprocessError) as error:
                ownership_result = {"resolved": False, "detail": str(error)}
            _drained(root, ledger, ownership_result)
            if had_database: backup(connection, archive / "resources.sqlite3")
            # Rebuild under this same transaction. A connection that opened the
            # old file before BEGIN IMMEDIATE sees no old tickets after it wakes.
            for table in ("requests", "invocations", "metadata"):
                connection.execute("DROP TABLE IF EXISTS " + table)
            initialize_ledger(connection, _scheduler_config(config), boot, generation, time.monotonic())
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction: connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        _write_generation(root, generation)
        replace(temporary, destination)
        return {"archive": str(archive), "config": str(destination), "boot_id": boot}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect")
    clean = commands.add_parser("cleanup"); clean.add_argument("--attempt", required=True)
    acknowledge = commands.add_parser("acknowledge-missing-result")
    acknowledge.add_argument("--attempt", required=True); acknowledge.add_argument("--reason", required=True)
    suite_ack = commands.add_parser("acknowledge-suite-parent")
    suite_ack.add_argument("--attempt", required=True); suite_ack.add_argument("--reason", required=True)
    migration = commands.add_parser("migrate"); migration.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "inspect": value = inspect(args.root)
    elif args.command == "cleanup": value = cleanup(args.root, args.attempt)
    elif args.command == "acknowledge-missing-result": value = acknowledge_missing_result(args.root, args.attempt, args.reason)
    elif args.command == "acknowledge-suite-parent": value = acknowledge_suite_parent(args.root, args.attempt, args.reason)
    else: value = migrate(args.root, args.config)
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RecoveryBlocked, ValueError, json.JSONDecodeError) as error:
        print("[pandora operator] " + str(error), file=sys.stderr)
        raise SystemExit(70)
