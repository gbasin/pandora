import unittest

from suite_evidence import aggregate, plan_digest, validate_plan, validate_shard


def plan():
    value = {"version": 1, "source_digest": "a" * 64, "selection": ["S0-01", "S1-02", "SX-03"],
             "catalog": [{"id": "S0-01", "consequential": True}, {"id": "S1-02", "consequential": False}, {"id": "SX-03", "consequential": True}],
             "replay_ids": ["S0-01", "SX-03"], "shards": [{"index": 1, "ids": ["S0-01", "S1-02"]}, {"index": 2, "ids": ["SX-03"]}]}
    value["plan_id"] = plan_digest(value)
    return value


def report(p, shard, rows, *, exit_code=0, unrun=None, infra=0, coverage=None):
    ids = next(item["ids"] for item in p["shards"] if item["index"] == shard)
    return {"version": 1, "plan_id": p["plan_id"], "source_digest": p["source_digest"], "shard": shard,
            "planned_ids": ids, "exit_code": exit_code, "results": rows,
            "errors": {"infrastructureFailures": infra, "unrunJourneys": unrun or []}, "coverage": coverage, "detail": "test"}


def result(identifier, status="pass", replayed=False):
    return {"id": identifier, "stage": identifier.split("-", 1)[0], "status": status, "replayed": replayed}


def coverage(p, shard, rows):
    ids = next(item["ids"] for item in p["shards"] if item["index"] == shard)
    selected = sorted(set(p["replay_ids"]) & set(ids))
    completed = sorted(row["id"] for row in rows if row.get("replayed") and row["id"] in selected)
    passed = sorted(row["id"] for row in rows if row.get("replayed") and row["status"] == "pass" and row["id"] in selected)
    return {"mode": "cover", "journeys": {"total": len(ids), "consequential": sum(item["consequential"] for item in p["catalog"] if item["id"] in ids), "selected": selected, "completed": completed, "passed": passed, "withoutReplayResult": sorted(set(selected) - set(completed))}, "stageRoutes": {key: [] for key in ("observed", "selected", "replayed", "passed", "uncovered")}}


class SuiteEvidenceTest(unittest.TestCase):
    def test_valid_plan_and_not_implemented_success(self):
        p = plan()
        rows_one = [result("S0-01", replayed=True), result("S1-02", "not-implemented")]
        rows_two = [result("SX-03", replayed=True)]
        one = report(p, 1, rows_one, coverage=coverage(p, 1, rows_one))
        two = report(p, 2, rows_two, coverage=coverage(p, 2, rows_two))
        output = aggregate(p, [two, one])
        self.assertEqual(output["status"], "pass")
        self.assertEqual([row["id"] for row in output["results"]], ["S0-01", "S1-02", "SX-03"])

    def test_plan_rejects_tampering_and_selection_mismatch(self):
        p = plan(); p["catalog"][0]["consequential"] = 1
        with self.assertRaises(ValueError): validate_plan(p)
        p = plan(); p["selection"] = ["S0-01", "S1-02"] ; p["plan_id"] = plan_digest(p)
        with self.assertRaises(ValueError): validate_plan(p)

    def test_report_rejects_identity_and_membership_tampering(self):
        p = plan(); r = report(p, 1, [result("S0-01"), result("S1-02")])
        r["source_digest"] = "b" * 64
        with self.assertRaises(ValueError): validate_shard(p, r)
        r = report(p, 1, [result("S0-01"), result("S1-02")]); r["planned_ids"] = ["S1-02", "S0-01"]
        with self.assertRaises(ValueError): validate_shard(p, r)

    def test_coverage_matches_replay_results(self):
        p = plan()
        rows = [result("S0-01", replayed=True), result("S1-02")]
        evidence = coverage(p, 1, rows)
        validate_shard(p, report(p, 1, rows, coverage=evidence))
        evidence["journeys"]["passed"] = []
        with self.assertRaises(ValueError): validate_shard(p, report(p, 1, rows, coverage=evidence))

    def test_actual_id_stage_and_coverage_contract(self):
        p = plan()
        p["catalog"][0]["id"] = "S0"
        p["plan_id"] = plan_digest(p)
        with self.assertRaises(ValueError): validate_plan(p)
        p = plan(); rows = [result("S0-01", replayed=True), result("S1-02")]
        evidence = coverage(p, 1, rows)
        evidence["mode"] = "all"
        with self.assertRaises(ValueError): validate_shard(p, report(p, 1, rows, coverage=evidence))
        bad_stage = result("S0-01"); bad_stage["stage"] = "S1"
        with self.assertRaises(ValueError): validate_shard(p, report(p, 1, [bad_stage, result("S1-02")], coverage=coverage(p, 1, rows)))
        leaking = result("S0-01"); leaking["fixture"] = "raw-ledger"
        with self.assertRaises(ValueError): validate_shard(p, report(p, 1, [leaking, result("S1-02")], coverage=coverage(p, 1, rows)))
        with self.assertRaises(ValueError): validate_shard(p, report(p, 1, rows))

    def test_partial_infrastructure_failure_preserved_and_missing_shards_rejected(self):
        p = plan()
        partial = report(p, 1, [result("S0-01", "pass")], exit_code=2, unrun=["S1-02"], infra=1)
        validate_shard(p, partial)
        with self.assertRaises(ValueError): aggregate(p, [partial])
        rows = [result("SX-03")]
        second = report(p, 2, rows, coverage=coverage(p, 2, rows))
        output = aggregate(p, [partial, second])
        self.assertEqual((output["status"], output["exit_code"]), ("fail", 1))
        self.assertEqual(output["errors"], [{"shard": 1, "exit_code": 2, "infrastructureFailures": 1,
                                              "unrunJourneys": ["S1-02"], "detail": "test"}])
        with self.assertRaises(ValueError): aggregate(p, [partial, partial])

    def test_duplicate_results_are_rejected_before_coverage_is_accepted(self):
        p = plan()
        rows = [result("S0-01", replayed=True), result("S0-01"), result("S1-02")]
        with self.assertRaises(ValueError): validate_shard(p, report(p, 1, rows, coverage=coverage(p, 1, rows)))


if __name__ == "__main__":
    unittest.main()
