import unittest

from suite_evidence import plan_digest
from suite_parent_evidence import summarize, validate_summary


ATTEMPTS = [f"{number:032x}" for number in range(1, 5)]


def plan():
    value = {"version": 1, "source_digest": "a" * 64, "selection": ["S0-01", "S1-02", "SX-03"],
             "catalog": [{"id": "S0-01", "consequential": True}, {"id": "S1-02", "consequential": False}, {"id": "SX-03", "consequential": True}],
             "replay_ids": [], "shards": [{"index": 1, "ids": ["S0-01"]}, {"index": 2, "ids": ["S1-02"]}, {"index": 3, "ids": ["SX-03"]}]}
    value["plan_id"] = plan_digest(value)
    return value


def row(identifier, status="pass"):
    return {"id": identifier, "stage": identifier.split("-", 1)[0], "status": status}


def report(p, shard, *, code=0, status="pass", infra=0, unrun=None):
    identifier = next(item["ids"][0] for item in p["shards"] if item["index"] == shard)
    return {"version": 1, "plan_id": p["plan_id"], "source_digest": p["source_digest"], "shard": shard,
            "planned_ids": [identifier], "exit_code": code, "results": [] if unrun else [row(identifier, status)],
            "errors": {"infrastructureFailures": infra, "unrunJourneys": unrun or []},
            "coverage": {"mode": "cover", "journeys": {"total": 1, "consequential": int(identifier != "S1-02"), "selected": [], "completed": [], "passed": [], "withoutReplayResult": []}, "stageRoutes": {key: [] for key in ("observed", "selected", "replayed", "passed", "uncovered")}} if code == 0 else None,
            "detail": "test"}


def summary(p, reports, **kwargs):
    keep_going = kwargs.pop("keep_going", False)
    return summarize(p, reports, parent_attempt=ATTEMPTS[0], plan_attempt=ATTEMPTS[1], shard_attempts=ATTEMPTS[2:] + ["0" * 32], keep_going=keep_going, **kwargs)


class SuiteParentEvidenceTest(unittest.TestCase):
    def test_complete_pass_and_validator(self):
        p = plan(); value = summary(p, [report(p, 1), report(p, 2), report(p, 3)])
        self.assertEqual((value["status"], value["exit_code"], value["unrun_shards"]), ("pass", 0, []))
        self.assertEqual(validate_summary(p, [report(p, 1), report(p, 2), report(p, 3)], value), value)

    def test_failfast_skips_tail_but_missing_report_is_not_silent(self):
        p = plan(); value = summary(p, [report(p, 1, code=1, status="fail")])
        self.assertEqual((value["stop_reason"], value["unrun_shards"], value["exit_code"]), ("test-failure", [2, 3], 1))
        with self.assertRaises(ValueError): summary(p, [report(p, 1)])

    def test_keep_going_collects_test_failures(self):
        p = plan(); value = summary(p, [report(p, 1, code=1, status="fail"), report(p, 2), report(p, 3)], keep_going=True)
        self.assertEqual((value["status"], value["stop_reason"]), ("fail", "test-failure"))
        with self.assertRaises(ValueError): summary(p, [report(p, 1, code=1, status="fail")], keep_going=True)
        with self.assertRaises(ValueError): summary(p, [report(p, 1, code=1, status="fail")], keep_going=True, stop_reason="test-failure")

    def test_reordered_plan_uses_shard_indices_and_deadline_beats_partial_infrastructure(self):
        p = plan(); p["shards"] = [p["shards"][2], p["shards"][0], p["shards"][1]]; p["plan_id"] = plan_digest(p)
        value = summary(p, [report(p, 1)], stop_reason="deadline")
        self.assertEqual((value["unrun_shards"], value["unrun_journeys"]), ([2, 3], ["S1-02", "SX-03"]))
        partial = report(p, 1, code=75, infra=1, unrun=["S0-01"])
        value = summary(p, [partial], stop_reason="deadline")
        self.assertEqual((value["stop_reason"], value["status"], value["exit_code"]), ("deadline", "stopped", 75))

    def test_rejects_bad_attempts_source_duplicate_reports_and_infrastructure_continuation(self):
        p = plan()
        with self.assertRaises(ValueError): summarize(p, [report(p, 1)], parent_attempt="bad", plan_attempt=ATTEMPTS[1], shard_attempts=ATTEMPTS[2:] + ["0" * 32], keep_going=False, stop_reason="deadline")
        bad = report(p, 1); bad["source_digest"] = "b" * 64
        with self.assertRaises(ValueError): summary(p, [bad], stop_reason="infrastructure")
        with self.assertRaises(ValueError): summary(p, [report(p, 1), report(p, 1)], stop_reason="deadline")
        with self.assertRaises(ValueError): summary(p, [report(p, 1, code=2, infra=1), report(p, 2)], stop_reason="infrastructure")
        with self.assertRaises(ValueError): summary(p, [], stop_reason=[])


if __name__ == "__main__":
    unittest.main()
