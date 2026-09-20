"""Authenticate a bounded suite run from its plan and shard reports.

The parent receipt is deliberately derived from the immutable plan and the
reports it received.  Attempt-receipt authentication is performed by the
caller; this module binds the reports to the plan and records what was not run.
"""

from __future__ import annotations

import re
from typing import Any

from suite_evidence import validate_plan, validate_shard


_ATTEMPT = re.compile(r"^[0-9a-f]{32}$")
_STOP_REASONS = {"test-failure", "infrastructure", "deadline", "queue-timeout", "cancelled"}
_KEYS = {
    "version", "parent_attempt", "source_digest", "plan_id", "plan_attempt",
    "shard_attempts", "keep_going", "stop_reason", "completed_shards",
    "unrun_shards", "unrun_journeys", "results", "exit_code", "status",
}


def _fail(message: str) -> None:
    raise ValueError(message)


def _attempt(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ATTEMPT.fullmatch(value):
        _fail(f"{name} must be a lower-case 32-hex attempt ID")
    return value


def _inputs(
    plan: Any, reports: Any, *, parent_attempt: Any, plan_attempt: Any,
    shard_attempts: Any, keep_going: Any, stop_reason: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]], str, str, list[str], bool, str | None]:
    validated_plan = validate_plan(plan)
    parent = _attempt(parent_attempt, "parent_attempt")
    plan_id = _attempt(plan_attempt, "plan_attempt")
    if not isinstance(shard_attempts, list) or len(shard_attempts) != len(validated_plan["shards"]):
        _fail("shard_attempts must have one attempt ID per planned shard")
    attempts = [_attempt(value, "shard_attempt") for value in shard_attempts]
    if len({parent, plan_id, *attempts}) != len(attempts) + 2:
        _fail("parent, plan, and shard attempts must be distinct")
    if type(keep_going) is not bool:
        _fail("keep_going must be boolean")
    if stop_reason is not None and (not isinstance(stop_reason, str) or stop_reason not in _STOP_REASONS):
        _fail("stop_reason is invalid")
    if not isinstance(reports, list):
        _fail("reports must be a list")
    if len(reports) > len(validated_plan["shards"]):
        _fail("reports exceed planned shards")
    validated_reports = [validate_shard(validated_plan, report) for report in reports]
    actual = [report["shard"] for report in validated_reports]
    expected = list(range(1, len(actual) + 1))
    if actual != expected:
        _fail("reports must be a sequential shard prefix")
    return validated_plan, validated_reports, parent, plan_id, attempts, keep_going, stop_reason


def summarize(
    plan: Any, reports: Any, *, parent_attempt: Any, plan_attempt: Any,
    shard_attempts: Any, keep_going: Any, stop_reason: Any = None,
) -> dict[str, Any]:
    """Return the canonical parent receipt for the received shard prefix."""
    plan, reports, parent, plan_attempt, shard_attempts, keep_going, requested = _inputs(
        plan, reports, parent_attempt=parent_attempt, plan_attempt=plan_attempt,
        shard_attempts=shard_attempts, keep_going=keep_going, stop_reason=stop_reason,
    )
    total = len(plan["shards"])
    completed = [report["shard"] for report in reports]
    unrun_shards = list(range(len(reports) + 1, total + 1))
    results = [row for report in reports for row in report["results"]]
    shards_by_index = {shard["index"]: shard for shard in plan["shards"]}
    unrun_journeys = [
        identifier for report in reports for identifier in report["errors"]["unrunJourneys"]
    ] + [
        identifier for index in unrun_shards for identifier in shards_by_index[index]["ids"]
    ]

    infrastructure = next((report for report in reports if report["exit_code"] != 1 and report["exit_code"] != 0
                           or report["errors"]["infrastructureFailures"]
                           or report["errors"]["unrunJourneys"]), None)
    failures = [index for index, report in enumerate(reports) if report["exit_code"] == 1
                and not report["errors"]["infrastructureFailures"]
                and not report["errors"]["unrunJourneys"]]

    external_stop = requested in {"deadline", "queue-timeout", "cancelled"}
    if infrastructure is not None:
        if reports[-1] is not infrastructure:
            _fail("infrastructure failure must stop the suite immediately")
        if requested not in (None, "infrastructure", "deadline", "queue-timeout", "cancelled"):
            _fail("infrastructure failure conflicts with stop_reason")
        resolved = requested if external_stop else "infrastructure"
    elif failures:
        first = failures[0]
        if not keep_going and first != len(reports) - 1:
            _fail("failfast must stop at the first test failure")
        if not keep_going and requested not in (None, "test-failure", "deadline", "queue-timeout", "cancelled"):
            _fail("failfast test failure conflicts with stop_reason")
        if keep_going and unrun_shards and not external_stop:
            _fail("keep-going missing shard reports require an external stop_reason")
        resolved = requested or "test-failure"
    elif requested is not None:
        if requested == "test-failure":
            _fail("test-failure stop_reason requires a test failure")
        if not unrun_shards and not external_stop:
            _fail("stop_reason requires an unrun shard")
        resolved = requested
    elif unrun_shards:
        _fail("missing shard reports require a stop_reason")
    else:
        resolved = None

    if resolved is None:
        status, exit_code = "pass", 0
    elif resolved == "test-failure":
        status, exit_code = "fail", 1
    elif resolved == "cancelled":
        status, exit_code = "stopped", 130
    else:
        status, exit_code = "stopped", 75
    return {
        "version": 1, "parent_attempt": parent, "source_digest": plan["source_digest"],
        "plan_id": plan["plan_id"], "plan_attempt": plan_attempt,
        "shard_attempts": shard_attempts, "keep_going": keep_going,
        "stop_reason": resolved, "completed_shards": completed,
        "unrun_shards": unrun_shards, "unrun_journeys": unrun_journeys,
        "results": results, "exit_code": exit_code, "status": status,
    }


def validate_summary(plan: Any, reports: Any, result: Any) -> dict[str, Any]:
    """Validate a parent receipt by deriving it again from its public inputs."""
    if not isinstance(result, dict) or set(result) != _KEYS:
        _fail("summary has an invalid schema")
    if result["version"] != 1 or type(result["version"]) is not int:
        _fail("summary version must be 1")
    expected = summarize(
        plan, reports, parent_attempt=result["parent_attempt"],
        plan_attempt=result["plan_attempt"], shard_attempts=result["shard_attempts"],
        keep_going=result["keep_going"], stop_reason=result["stop_reason"],
    )
    if result != expected:
        _fail("summary does not match plan and reports")
    return result
