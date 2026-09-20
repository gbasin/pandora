import json
import fcntl
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import operator_recovery as recovery
from resource_admission import Scheduler, SchedulerUnavailable


IDENTITY = "a" * 32
CONFIG = {"version": 1,
          "scheduler": {"version": 1, "policy": "fifo", "max_running": 2,
                        "cpu_millis": 4000, "memory_mib": 8192, "disk_mib": 4096, "disk_floor_mib": 100},
          "max_parallel": 2, "execution_seconds": 1200, "workspace_mib": 1024,
          "limits": {"main": {"cpu_millis": 2000, "memory_mib": 6144},
                     "db": {"cpu_millis": 500, "memory_mib": 768},
                     "pool": {"cpu_millis": 500, "memory_mib": 256},
                     "proxy": {"cpu_millis": 500, "memory_mib": 128}}}


class OperatorRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.attempt = self.root / "runs" / IDENTITY
        self.attempt.mkdir(parents=True)
        (self.attempt / "attempt.lock").touch()
        (self.attempt / "service_cleanup.py").write_text("# copied worker cleanup\n")
        (self.attempt / "submission.json").write_text(json.dumps({"attempt": IDENTITY, "workflow": "surface", "source_digest": "d" * 64}))

    def tearDown(self): self.temp.cleanup()

    def ledger(self, phase="running"):
        db = sqlite3.connect(self.root / "resources.sqlite3")
        try:
            db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE requests (ticket INTEGER PRIMARY KEY, attempt TEXT, invocation TEXT, phase TEXT)")
            db.execute("INSERT INTO metadata VALUES ('config', ?)", (json.dumps(CONFIG, sort_keys=True),))
            db.execute("INSERT INTO metadata VALUES ('boot_id', 'old')")
            db.execute("INSERT INTO requests VALUES (1, ?, ?, ?)", (IDENTITY, "b" * 32, phase))
            db.commit()
        finally:
            db.close()

    def receipt(self, name):
        if name == "terminal.json":
            (self.attempt / "artifacts.json").write_text("{}")
            value = {"attempt": IDENTITY, "workflow": "surface", "exit_code": 70, "cleanup_verified": True}
        else:
            value = {"attempt": IDENTITY, "cleanup_verified": True}
        (self.attempt / name).write_text(json.dumps(value))

    def test_inspect_uses_readonly_ledger_without_creating_missing_database(self):
        value = recovery.inspect(self.root, ownership=lambda root, admitted: None)
        self.assertFalse(value["ledger"]["present"])
        self.assertFalse((self.root / "resources.sqlite3").exists())

    def test_inspect_passes_only_live_running_rows_to_ownership_check(self):
        self.ledger(phase="waiting")
        seen = []
        recovery.inspect(self.root, ownership=lambda root, admitted: seen.append(admitted))
        self.assertEqual(seen, [set()])

    def test_cleanup_rejects_live_owner_before_bundled_entrypoint(self):
        with patch("operator_recovery.alive", return_value=True), patch("operator_recovery.subprocess.run") as run:
            with self.assertRaisesRegex(recovery.RecoveryBlocked, "live"):
                recovery.cleanup(self.root, IDENTITY)
        run.assert_not_called()

    def test_cleanup_rejects_foreign_or_unlabelled_managed_resource(self):
        foreign = [{"Names": "pandora-warm-" + IDENTITY, "Labels": "pandora.attempt=" + "b" * 32}]
        with self.assertRaisesRegex(recovery.RecoveryBlocked, "Foreign or unlabelled"):
            recovery.cleanup(self.root, IDENTITY, list_resources=lambda args: foreign if args[0] == "ps" else [])

    def test_acknowledgement_never_writes_terminal_and_is_immutable(self):
        self.receipt("admission-cleanup.json")
        value = recovery.acknowledge_missing_result(self.root, IDENTITY, "host-reboot", now=lambda: 1, list_resources=lambda args: [])
        self.assertEqual(value["state"], "infrastructure-failed")
        self.assertFalse((self.attempt / "terminal.json").exists())
        self.assertEqual(value, recovery.acknowledge_missing_result(self.root, IDENTITY, "again", list_resources=lambda args: []))

    def test_unusable_terminal_is_retained_and_bound_but_valid_terminal_is_rejected(self):
        self.receipt("admission-cleanup.json")
        terminal = self.attempt / "terminal.json"; terminal.write_text("broken evidence\n")
        result = recovery.acknowledge_missing_result(self.root, IDENTITY, "worker-loss", list_resources=lambda args: [])
        self.assertEqual(result["terminal_sha256"], __import__("hashlib").sha256(terminal.read_bytes()).hexdigest())
        self.assertEqual(terminal.read_text(), "broken evidence\n")
        other = "b" * 32; attempt = self.root / "runs" / other; attempt.mkdir()
        (attempt / "attempt.lock").touch(); (attempt / "submission.json").write_text(json.dumps({"attempt": other, "workflow": "surface", "source_digest": "e" * 64}))
        (attempt / "admission-cleanup.json").write_text(json.dumps({"attempt": other, "cleanup_verified": True}))
        (attempt / "artifacts.json").write_text("{}")
        (attempt / "terminal.json").write_text(json.dumps({"attempt": other, "workflow": "surface", "exit_code": 70, "cleanup_verified": True}))
        with self.assertRaisesRegex(recovery.RecoveryBlocked, "valid terminal"):
            recovery.acknowledge_missing_result(self.root, other, "worker-loss", list_resources=lambda args: [])

    def test_acknowledgement_requires_cleanup_and_dead_owner(self):
        with self.assertRaisesRegex(recovery.RecoveryBlocked, "verified cleanup"):
            recovery.acknowledge_missing_result(self.root, IDENTITY, "host-reboot", list_resources=lambda args: [])
        self.receipt("admission-cleanup.json")
        with patch("operator_recovery.alive", return_value=True):
            with self.assertRaisesRegex(recovery.RecoveryBlocked, "dead owner"):
                recovery.acknowledge_missing_result(self.root, IDENTITY, "host-reboot", list_resources=lambda args: [])

    def test_acknowledgement_rejects_remaining_exact_resource(self):
        self.receipt("admission-cleanup.json")
        resource = [{"Names": "pandora-warm-" + IDENTITY,
                     "Labels": "pandora.workflow=surface,pandora.experiment=warm-surface,pandora.attempt=" + IDENTITY}]
        with self.assertRaisesRegex(recovery.RecoveryBlocked, "resources remain"):
            recovery.acknowledge_missing_result(self.root, IDENTITY, "host-reboot",
                                                list_resources=lambda args: resource if args[0] == "ps" else [])

    def test_migration_accepts_stale_verified_terminal_without_scheduler(self):
        self.ledger(); self.receipt("terminal.json")
        config = self.root / "new.json"; config.write_text(json.dumps(CONFIG))
        value = recovery.migrate(self.root, config, boot_id="new", ownership=lambda root, admitted: None)
        self.assertTrue((Path(value["archive"]) / "resources.sqlite3").exists())
        self.assertEqual(json.loads((self.root / "worker-config.json").read_text()), CONFIG)
        db = sqlite3.connect(self.root / "resources.sqlite3")
        try:
            self.assertEqual(dict(db.execute("SELECT key, value FROM metadata"))["boot_id"], "new")
        finally:
            db.close()

    def test_migration_rebuilds_disk_schema_and_fences_a_stale_scheduler(self):
        self.ledger(); self.receipt("terminal.json")
        stale = Scheduler(self.root, CONFIG["scheduler"], boot_id="old")
        config = self.root / "new.json"; config.write_text(json.dumps(CONFIG))
        recovery.migrate(self.root, config, boot_id="new", ownership=lambda root, admitted: None)
        with self.assertRaises(SchedulerUnavailable): stale.snapshot()
        fresh = Scheduler(self.root, CONFIG["scheduler"], boot_id="new")
        fresh.register("b" * 32)
        attempt = self.root / "runs" / ("b" * 32)
        attempt.mkdir()
        handle = (attempt / "attempt.lock").open("a")
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            fresh.enqueue(attempt.name, "b" * 32,
                          {"cpu_millis": 500, "memory_mib": 512, "disk_mib": 100, "exclusive": []})
            lease = fresh.claim(attempt.name)
            self.assertIsNotNone(lease)
            lease.close()
        finally:
            handle.close()

    def test_migration_blocks_unacknowledged_missing_running_result(self):
        self.ledger(); config = self.root / "new.json"; config.write_text(json.dumps(CONFIG))
        with self.assertRaisesRegex(recovery.RecoveryBlocked, "lacks a terminal"):
            recovery.migrate(self.root, config, boot_id="new", ownership=lambda root, admitted: None)

    def test_migration_blocks_live_waiting_attempt(self):
        self.ledger(phase="waiting"); config = self.root / "new.json"; config.write_text(json.dumps(CONFIG))
        with patch("operator_recovery.alive", return_value=True):
            with self.assertRaisesRegex(recovery.RecoveryBlocked, "Live attempt"):
                recovery.migrate(self.root, config, boot_id="new", ownership=lambda root, admitted: None)

    def test_migration_accepts_retained_finished_row_after_attempt_evidence_ages_out(self):
        self.ledger(phase="finished")
        for item in self.attempt.iterdir(): item.unlink()
        self.attempt.rmdir()
        config = self.root / "new.json"; config.write_text(json.dumps(CONFIG))
        recovery.migrate(self.root, config, boot_id="new", ownership=lambda root, admitted: None)
        self.assertTrue((self.root / "worker-config.json").exists())

    def test_no_ledger_migration_still_rejects_a_live_attempt(self):
        config = self.root / "new.json"; config.write_text(json.dumps(CONFIG))
        with patch("operator_recovery.alive", return_value=True):
            with self.assertRaisesRegex(recovery.RecoveryBlocked, "Live attempt"):
                recovery.migrate(self.root, config, boot_id="new", ownership=lambda root, admitted: None)

    def test_migration_accepts_acknowledged_missing_result(self):
        self.ledger(); self.receipt("admission-cleanup.json")
        recovery.acknowledge_missing_result(self.root, IDENTITY, "host-reboot", list_resources=lambda args: [])
        config = self.root / "new.json"; config.write_text(json.dumps(CONFIG))
        recovery.migrate(self.root, config, boot_id="new", ownership=lambda root, admitted: None)
        self.assertTrue((self.root / "worker-config.json").exists())

    def test_migration_rejects_a_shape_valid_but_unbound_acknowledgement(self):
        self.ledger(); self.receipt("admission-cleanup.json")
        result = recovery.acknowledge_missing_result(self.root, IDENTITY, "host-reboot", list_resources=lambda args: [])
        result["submission_sha256"] = "0" * 64
        (self.attempt / "operator-result.json").write_text(json.dumps(result))
        config = self.root / "new.json"; config.write_text(json.dumps(CONFIG))
        with self.assertRaisesRegex(recovery.RecoveryBlocked, "lacks a terminal"):
            recovery.migrate(self.root, config, boot_id="new", ownership=lambda root, admitted: None)

    def test_config_install_failure_leaves_archive_and_fails_closed(self):
        self.ledger(); self.receipt("terminal.json")
        config = self.root / "new.json"; config.write_text(json.dumps(CONFIG))
        def fail(source, target): raise OSError("disk interrupted")
        with self.assertRaisesRegex(OSError, "interrupted"):
            recovery.migrate(self.root, config, boot_id="new", replace=fail, ownership=lambda root, admitted: None)
        self.assertFalse((self.root / "worker-config.json").exists())
        self.assertEqual(len(list((self.root / "operator-archive").iterdir())), 1)
        db = sqlite3.connect(self.root / "resources.sqlite3")
        try:
            self.assertEqual(dict(db.execute("SELECT key, value FROM metadata"))["boot_id"], "new")
        finally:
            db.close()

    def test_binding_requires_submission_attempt_and_explicit_workflow(self):
        self.receipt("admission-cleanup.json")
        (self.attempt / "submission.json").write_text(json.dumps({"source_digest": "d" * 64}))
        with self.assertRaisesRegex(recovery.RecoveryBlocked, "immutable attempt identity"):
            recovery.acknowledge_missing_result(self.root, IDENTITY, "host-reboot", list_resources=lambda args: [])

    def suite_child(self, identity, *, acknowledged=False, terminal=False):
        path = self.root / "runs" / identity
        path.mkdir()
        (path / "attempt.lock").touch()
        (path / "service_cleanup.py").write_text("# copied worker cleanup\n")
        (path / "submission.json").write_text(json.dumps({"attempt": identity, "parent_attempt": IDENTITY,
                                                              "workflow": "surface", "source_digest": identity * 2}))
        if terminal:
            (path / "artifacts.json").write_text("{}")
            (path / "terminal.json").write_text(json.dumps({"attempt": identity, "workflow": "surface", "exit_code": 70,
                                                               "cleanup_verified": True}))
        if acknowledged:
            (path / "admission-cleanup.json").write_text(json.dumps({"attempt": identity, "cleanup_verified": True}))
            recovery.acknowledge_missing_result(self.root, identity, "host-reboot", now=lambda: 1, list_resources=lambda args: [])
        return path

    def suite_parent(self, children):
        (self.attempt / "submission.json").write_text(json.dumps({"attempt": IDENTITY, "workflow": "suite-run", "source_digest": "d" * 64}))
        (self.attempt / "suite-cleanup.pending").touch()
        (self.attempt / "children.json").write_text(json.dumps({"version": 1, "parent_attempt": IDENTITY, "children": children}))

    def test_suite_parent_rejects_live_parent_before_child_cleanup(self):
        child = self.suite_child("b" * 32)
        self.suite_parent([child.name])
        with patch("operator_recovery.alive", side_effect=lambda attempt: Path(attempt) == self.attempt), \
                patch("operator_recovery.cleanup") as clean:
            with self.assertRaisesRegex(recovery.RecoveryBlocked, "Live suite parent"):
                recovery.acknowledge_suite_parent(self.root, IDENTITY, "host-reboot", list_resources=lambda args: [])
        clean.assert_not_called()

    def test_suite_parent_resumes_mixed_terminal_and_acknowledged_children(self):
        terminal = self.suite_child("b" * 32, terminal=True)
        acknowledged = self.suite_child("c" * 32, acknowledged=True)
        self.suite_parent([terminal.name, acknowledged.name])
        calls = []
        def clean(root, identity, **kwargs):
            calls.append(identity)
            path = root / "runs" / identity
            (path / "admission-cleanup.json").write_text(json.dumps({"attempt": identity, "cleanup_verified": True}))
            (path / "suite-cleanup.pending").unlink(missing_ok=True)
            return recovery._state(root, identity)
        with patch("operator_recovery.cleanup", side_effect=clean):
            result = recovery.acknowledge_suite_parent(self.root, IDENTITY, "host-reboot", now=lambda: 3, list_resources=lambda args: [])
        self.assertEqual(calls, [IDENTITY])
        self.assertEqual(result["state"], "infrastructure-failed")
        self.assertTrue((self.attempt / "operator-cleanup.json").exists())
        # A retry retains each immutable receipt and performs no child cleanup.
        with patch("operator_recovery.cleanup", side_effect=clean) as cleanup:
            retry = recovery.acknowledge_suite_parent(self.root, IDENTITY, "different", now=lambda: 4, list_resources=lambda args: [])
        self.assertEqual(retry, result)
        self.assertEqual([item.args[1] for item in cleanup.call_args_list], [IDENTITY])

    def test_suite_parent_resumes_after_parent_cleanup_receipt_before_acknowledgement(self):
        child = self.suite_child("b" * 32, acknowledged=True)
        self.suite_parent([child.name])
        (self.attempt / "admission-cleanup.json").write_text(json.dumps({"attempt": IDENTITY, "cleanup_verified": True}))
        (self.attempt / "suite-cleanup.pending").unlink()
        stored = {"parent_attempt": IDENTITY, "children": [child.name], "cleanup_verified": True, "acknowledged_at": 7}
        (self.attempt / "operator-cleanup.json").write_text(json.dumps(stored))
        def clean(*args, **kwargs):
            return recovery._state(self.root, IDENTITY)
        with patch("operator_recovery.cleanup", side_effect=clean):
            result = recovery.acknowledge_suite_parent(self.root, IDENTITY, "host-reboot", now=lambda: 8, list_resources=lambda args: [])
        self.assertEqual(json.loads((self.attempt / "operator-cleanup.json").read_text()), stored)
        self.assertEqual(result["state"], "infrastructure-failed")

    def test_suite_parent_rechecks_bound_child_acknowledgement(self):
        child = self.suite_child("b" * 32, acknowledged=True)
        self.suite_parent([child.name])
        receipt = json.loads((child / "operator-result.json").read_text())
        receipt["submission_sha256"] = "0" * 64
        (child / "operator-result.json").write_text(json.dumps(receipt))
        with self.assertRaisesRegex(recovery.RecoveryBlocked, "immutable"):
            recovery.acknowledge_suite_parent(self.root, IDENTITY, "host-reboot", list_resources=lambda args: [])


if __name__ == "__main__": unittest.main()
