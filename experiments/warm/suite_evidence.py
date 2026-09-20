"""Validate the immutable suite plan and the evidence returned by each shard."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


_ID = re.compile(r"^(?:S[0-6]|SX)-[0-9]{2}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_STATUSES = {"pass", "fail", "not-implemented", "stopped"}


def _fail(message: str) -> None:
    raise ValueError(message)


def _object(value: Any, keys: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail(f"{name} has an invalid schema")
    return value


def _integer(value: Any, name: str, *, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        _fail(f"{name} must be an integer" + (f" >= {minimum}" if minimum is not None else ""))
    return value


def _id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail(f"{name} is not a scenario ID")
    return value


def _ids(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        _fail(f"{name} must be " + ("a nonempty " if nonempty else "a ") + "list")
    ids = [_id(item, name) for item in value]
    if len(ids) != len(set(ids)):
        _fail(f"{name} contains duplicates")
    return ids


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        _fail(f"{name} must be lower-case SHA-256 hex")
    return value


def plan_digest(plan: dict[str, Any]) -> str:
    """Return the canonical digest, excluding the self-referential ``plan_id``."""
    if not isinstance(plan, dict):
        _fail("plan must be an object")
    body = {key: value for key, value in plan.items() if key != "plan_id"}
    try:
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    except (TypeError, UnicodeEncodeError) as exc:
        raise ValueError("plan cannot be canonically encoded") from exc
    return hashlib.sha256(encoded).hexdigest()


def validate_plan(plan: Any) -> dict[str, Any]:
    plan = _object(plan, {"version", "source_digest", "selection", "catalog", "replay_ids", "shards", "plan_id"}, "plan")
    if plan["version"] != 1 or type(plan["version"]) is not int:
        _fail("plan version must be 1")
    _digest(plan["source_digest"], "source_digest")
    catalog = plan["catalog"]
    if not isinstance(catalog, list) or not catalog:
        _fail("catalog must be a nonempty list")
    catalog_ids: list[str] = []
    consequential: set[str] = set()
    for item in catalog:
        item = _object(item, {"id", "consequential"}, "catalog entry")
        identifier = _id(item["id"], "catalog id")
        if type(item["consequential"]) is not bool:
            _fail("catalog consequential must be boolean")
        catalog_ids.append(identifier)
        if item["consequential"]:
            consequential.add(identifier)
    if len(catalog_ids) != len(set(catalog_ids)):
        _fail("catalog contains duplicate IDs")
    selection = plan["selection"]
    if selection is not None:
        if set(_ids(selection, "selection")) != set(catalog_ids) or len(selection) != len(catalog_ids):
            _fail("selection must contain exactly the catalog IDs")
    replay_ids = _ids(plan["replay_ids"], "replay_ids")
    if not set(replay_ids) <= consequential:
        _fail("replay_ids must be consequential catalog IDs")
    shards = plan["shards"]
    if not isinstance(shards, list) or not shards or len(shards) > 32:
        _fail("shards must contain between 1 and 32 entries")
    shard_ids: list[str] = []
    indices: list[int] = []
    for shard in shards:
        shard = _object(shard, {"index", "ids"}, "shard")
        indices.append(_integer(shard["index"], "shard index", minimum=1))
        shard_ids.extend(_ids(shard["ids"], "shard ids", nonempty=True))
    if sorted(indices) != list(range(1, len(shards) + 1)):
        _fail("shard indices must be contiguous and unique")
    if len(shard_ids) != len(set(shard_ids)) or set(shard_ids) != set(catalog_ids):
        _fail("shards must partition the catalog")
    _digest(plan["plan_id"], "plan_id")
    if plan["plan_id"] != plan_digest(plan):
        _fail("plan_id does not match the plan")
    return plan


def _result(value: Any, known_ids: set[str]) -> dict[str, Any]:
    allowed = {"id", "stage", "status", "detail", "durationMs", "replayed", "writeRoutes", "replayWriteRoutes"}
    if not isinstance(value, dict) or not set(value) <= allowed or not {"id", "stage", "status"} <= set(value):
        _fail("result has an invalid schema")
    identifier = _id(value["id"], "result id")
    if identifier not in known_ids:
        _fail("result references a foreign ID")
    if value["stage"] != identifier.split("-", 1)[0] or not isinstance(value["status"], str) or value["status"] not in _STATUSES:
        _fail("result has an invalid stage or status")
    for key in ("detail",):
        if key in value and not isinstance(value[key], str): _fail(f"result {key} must be a string")
    if "durationMs" in value: _integer(value["durationMs"], "result durationMs", minimum=0)
    if "replayed" in value and type(value["replayed"]) is not bool: _fail("result replayed must be boolean")
    for key in ("writeRoutes", "replayWriteRoutes"):
        if key in value and (not isinstance(value[key], list) or not all(isinstance(route, str) for route in value[key])):
            _fail(f"result {key} must be a list of strings")
    return value


def _coverage(value: Any, plan: dict[str, Any], shard_ids: list[str], results: list[dict[str, Any]]) -> None:
    coverage = _object(value, {"mode", "journeys", "stageRoutes"}, "coverage")
    if coverage["mode"] != "cover": _fail("coverage mode must be cover")
    journeys = _object(coverage["journeys"], {"total", "consequential", "selected", "completed", "passed", "withoutReplayResult"}, "coverage journeys")
    stage_routes = _object(coverage["stageRoutes"], {"observed", "selected", "replayed", "passed", "uncovered"}, "coverage stageRoutes")
    catalog = plan["catalog"]
    expected_selected = sorted(set(plan["replay_ids"]) & set(shard_ids))
    expected_completed = sorted(row["id"] for row in results if row.get("replayed") and row["id"] in expected_selected)
    expected_passed = sorted(row["id"] for row in results if row.get("replayed") and row["status"] == "pass" and row["id"] in expected_selected)
    for key in ("total", "consequential"):
        _integer(journeys[key], f"coverage journeys {key}", minimum=0)
    shard_catalog = [item for item in catalog if item["id"] in shard_ids]
    if journeys["total"] != len(shard_ids) or journeys["consequential"] != sum(item["consequential"] for item in shard_catalog):
        _fail("coverage journey totals do not match plan")
    actual_sets = {"selected": expected_selected, "completed": expected_completed, "passed": expected_passed,
                   "withoutReplayResult": sorted(set(expected_selected) - set(expected_completed))}
    for key, expected in actual_sets.items():
        actual = _ids(journeys[key], f"coverage journeys {key}")
        if actual != expected: _fail(f"coverage journeys {key} does not match results")
    for key in stage_routes:
        if not isinstance(stage_routes[key], list) or not all(isinstance(route, str) for route in stage_routes[key]):
            _fail(f"coverage stageRoutes {key} must be a list of strings")


def validate_shard(plan: Any, report: Any) -> dict[str, Any]:
    plan = validate_plan(plan)
    report = _object(report, {"version", "plan_id", "source_digest", "shard", "planned_ids", "exit_code", "results", "errors", "coverage", "detail"}, "shard report")
    if report["version"] != 1 or type(report["version"]) is not int: _fail("report version must be 1")
    if report["plan_id"] != plan["plan_id"] or report["source_digest"] != plan["source_digest"]: _fail("report identity does not match plan")
    shard = _integer(report["shard"], "report shard", minimum=1)
    expected = next((item["ids"] for item in plan["shards"] if item["index"] == shard), None)
    if expected is None: _fail("report references a foreign shard")
    if _ids(report["planned_ids"], "planned_ids", nonempty=True) != expected: _fail("planned_ids do not match shard plan")
    exit_code = _integer(report["exit_code"], "exit_code", minimum=0)
    if not isinstance(report["results"], list): _fail("results must be a list")
    known = {item["id"] for item in plan["catalog"]}
    results = [_result(row, known) for row in report["results"]]
    result_ids = [row["id"] for row in results]
    if len(result_ids) != len(set(result_ids)) or not set(result_ids) <= set(expected): _fail("results must be unique shard IDs")
    errors = _object(report["errors"], {"infrastructureFailures", "unrunJourneys"}, "errors")
    infra = _integer(errors["infrastructureFailures"], "infrastructureFailures", minimum=0)
    unrun = _ids(errors["unrunJourneys"], "unrunJourneys")
    if set(result_ids) & set(unrun) or set(result_ids) | set(unrun) != set(expected): _fail("results and unrunJourneys must exactly cover the shard")
    if not isinstance(report["detail"], str): _fail("detail must be a string")
    if report["coverage"] is not None: _coverage(report["coverage"], plan, expected, results)
    elif exit_code == 0: _fail("zero exit report requires coverage")
    if exit_code == 0 and (infra or unrun or not any(row["status"] == "pass" for row in results) or any(row["status"] in {"fail", "stopped"} for row in results)):
        _fail("zero exit report has unsuccessful outcomes")
    return report


def aggregate(plan: Any, reports: Any) -> dict[str, Any]:
    plan = validate_plan(plan)
    if not isinstance(reports, list): _fail("reports must be a list")
    validated = [validate_shard(plan, report) for report in reports]
    expected = {shard["index"] for shard in plan["shards"]}
    actual = [report["shard"] for report in validated]
    if len(actual) != len(set(actual)) or set(actual) != expected: _fail("reports must contain every planned shard exactly once")
    rows = sorted((row for report in validated for row in report["results"]), key=lambda row: row["id"])
    failed = any(report["exit_code"] != 0 for report in validated)
    errors = [
        {"shard": report["shard"], "exit_code": report["exit_code"],
         "infrastructureFailures": report["errors"]["infrastructureFailures"],
         "unrunJourneys": report["errors"]["unrunJourneys"], "detail": report["detail"]}
        for report in validated if report["exit_code"] != 0
    ]
    return {"plan_id": plan["plan_id"], "source_digest": plan["source_digest"], "status": "fail" if failed else "pass", "exit_code": 1 if failed else 0, "results": rows, "shards": sorted(actual), "errors": errors}
