import unittest

from scheduling_policy import choose, validate_config, validate_demand


def config(**changes):
    return {"version": 1, "cpu_millis": 1000, "memory_mib": 1000,
            "max_running": 4, "policy": "fair"} | changes


def request(ticket, attempt, invocation, phase="waiting", cpu=100, memory=100):
    return {"ticket": ticket, "attempt": attempt, "invocation": invocation,
            "phase": phase, "cpu_millis": cpu, "memory_mib": memory}


def invocation(*, max_parallel=2, turn=0, stopped=False):
    return {"max_parallel": max_parallel, "turn": turn, "stopped": stopped}


class SchedulingPolicyTests(unittest.TestCase):
    def test_fair_gives_unserved_focused_invocation_a_turn_before_suite_backlog(self):
        invocations = {"suite": invocation(turn=4), "focused": invocation(turn=0)}
        requests = [request(1, "suite-one", "suite"), request(2, "suite-two", "suite"),
                    request(3, "focused-one", "focused")]
        self.assertEqual(choose(config(), requests, invocations), "focused-one")

    def test_fair_cap_reached_allows_another_invocation(self):
        invocations = {"suite": invocation(max_parallel=1, turn=0), "focused": invocation(turn=3)}
        requests = [request(1, "suite-running", "suite", "running"),
                    request(2, "suite-waiting", "suite"), request(3, "focused-waiting", "focused")]
        self.assertEqual(choose(config(), requests, invocations), "focused-waiting")

    def test_running_resource_use_bounds_cpu_and_memory(self):
        invocations = {"one": invocation(), "two": invocation(turn=1)}
        self.assertIsNone(choose(config(cpu_millis=1000, memory_mib=1000), [
            request(1, "running", "one", "running", cpu=900, memory=300),
            request(2, "waiting", "two", cpu=200, memory=800),
        ], invocations))
        self.assertIsNone(choose(config(cpu_millis=1000, memory_mib=1000), [
            request(1, "running", "one", "running", cpu=300, memory=900),
            request(2, "waiting", "two", cpu=600, memory=200),
        ], invocations))

    def test_fifo_head_blocks_later_request_when_it_cannot_fit(self):
        invocations = {"one": invocation(), "two": invocation(turn=1)}
        requests = [request(1, "running", "one", "running", cpu=800),
                    request(2, "head", "two", cpu=300), request(3, "later", "two", cpu=100)]
        self.assertIsNone(choose(config(policy="fifo"), requests, invocations))

    def test_fair_does_not_backfill_when_selected_head_cannot_fit(self):
        invocations = {"one": invocation(), "two": invocation(turn=1), "three": invocation(turn=2)}
        requests = [request(1, "running", "one", "running", cpu=800),
                    request(2, "selected-head", "two", cpu=300),
                    request(3, "later-fit", "three", cpu=100)]
        self.assertIsNone(choose(config(), requests, invocations))

    def test_exclusive_builders_and_disk_are_reserved_with_cpu_and_ram(self):
        settings = config(disk_mib=1000)
        invocations = {"one": invocation(), "two": invocation()}
        running = request(1, "running", "one", "running") | {"exclusive": ["dependency-builder"], "disk_mib": 600}
        pending = request(2, "pending", "two") | {"exclusive": ["dependency-builder"], "disk_mib": 300}
        self.assertIsNone(choose(settings, [running, pending], invocations))
        pending["exclusive"] = []
        self.assertEqual(choose(settings, [running, pending], invocations), "pending")
        pending["disk_mib"] = 500
        self.assertIsNone(choose(settings, [running, pending], invocations))

    def test_rejects_malformed_config_and_demands_larger_than_capacity(self):
        invocations = {"one": invocation()}
        with self.assertRaisesRegex(ValueError, "schema"):
            choose({"version": 1}, [], invocations)
        with self.assertRaisesRegex(ValueError, "max_running"):
            choose(config(max_running=33), [], invocations)
        with self.assertRaisesRegex(ValueError, "demand"):
            choose(config(), [request(1, "too-big", "one", cpu=1001)], invocations)
        with self.assertRaisesRegex(ValueError, "request cpu_millis"):
            choose(config(), [request(1, "invalid", "one", cpu=True)], invocations)

    def test_public_validators_require_exact_fit_values_and_handle_unhashable_enums(self):
        value = config()
        self.assertIs(validate_config(value), value)
        demand = {"cpu_millis": 100, "memory_mib": 200}
        self.assertIs(validate_demand(demand, value), demand)
        with self.assertRaisesRegex(ValueError, "demand"):
            validate_demand({"cpu_millis": 100, "memory_mib": 200, "extra": True}, value)
        with self.assertRaisesRegex(ValueError, "capacity"):
            validate_demand({"cpu_millis": 1001, "memory_mib": 1}, value)
        with self.assertRaisesRegex(ValueError, "policy"):
            validate_config(config(policy=[]))
        with self.assertRaisesRegex(ValueError, "phase"):
            choose(value, [request(1, "bad-phase", "one", phase=[])], {"one": invocation()})


if __name__ == "__main__":
    unittest.main()
