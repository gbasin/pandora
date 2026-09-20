"""Pure bounded-resource admission policy for remote scheduler attempts.

``choose`` selects one waiting attempt.  It does not mutate turns or request
state: the durable admission transaction records the next turn after it admits
the returned attempt.
"""

from __future__ import annotations

from typing import Any


_CONFIG_KEYS = {"version", "cpu_millis", "memory_mib", "max_running", "policy"}
_REQUEST_KEYS = {"ticket", "attempt", "invocation", "phase", "cpu_millis", "memory_mib"}
_INVOCATION_KEYS = {"max_parallel", "turn", "stopped"}


def _integer(value: Any, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def validate_config(value: Any) -> dict[str, Any]:
    """Validate and return the immutable scheduler resource configuration."""
    if not isinstance(value, dict) or not _CONFIG_KEYS <= set(value) or set(value) - _CONFIG_KEYS - {"disk_mib", "disk_floor_mib"}:
        raise ValueError("config has an invalid schema")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("config version must be 1")
    _integer(value["cpu_millis"], "config cpu_millis", minimum=1)
    _integer(value["memory_mib"], "config memory_mib", minimum=1)
    _integer(value["max_running"], "config max_running", minimum=1)
    if value["max_running"] > 32:
        raise ValueError("config max_running must not exceed 32")
    if not isinstance(value["policy"], str) or value["policy"] not in {"fair", "fifo"}:
        raise ValueError("config policy must be fair or fifo")
    for key in ("disk_mib", "disk_floor_mib"):
        if key in value:
            _integer(value[key], "config " + key, minimum=1)
    return value


def validate_demand(value: Any, config: Any) -> dict[str, Any]:
    """Validate one declared CPU/RAM demand against scheduler capacity."""
    checked_config = validate_config(config)
    if not isinstance(value, dict) or not {"cpu_millis", "memory_mib"} <= set(value) or set(value) - {"cpu_millis", "memory_mib", "disk_mib", "exclusive"}:
        raise ValueError("demand has an invalid schema")
    cpu = _integer(value["cpu_millis"], "demand cpu_millis", minimum=1)
    memory = _integer(value["memory_mib"], "demand memory_mib", minimum=1)
    if cpu > checked_config["cpu_millis"] or memory > checked_config["memory_mib"]:
        raise ValueError("demand exceeds scheduler capacity")
    disk = _integer(value.get("disk_mib", 0), "demand disk_mib", minimum=0)
    if disk > checked_config.get("disk_mib", 0):
        raise ValueError("demand exceeds disk reservation capacity")
    exclusive = value.get("exclusive", [])
    if (not isinstance(exclusive, list)
            or any(not isinstance(item, str) or item not in ("dependency-builder", "docker-builder") for item in exclusive)
            or len(set(exclusive)) != len(exclusive)):
        raise ValueError("Invalid exclusive resource demand")
    return value


def _invocations(value: Any, max_running: int) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("invocations must be a dictionary")
    result: dict[str, dict[str, Any]] = {}
    for identifier, invocation in value.items():
        _identifier(identifier, "invocation ID")
        if not isinstance(invocation, dict) or set(invocation) != _INVOCATION_KEYS:
            raise ValueError("invocation has an invalid schema")
        _integer(invocation["max_parallel"], "invocation max_parallel", minimum=1)
        if invocation["max_parallel"] > max_running:
            raise ValueError("invocation max_parallel exceeds scheduler capacity")
        _integer(invocation["turn"], "invocation turn", minimum=0)
        if type(invocation["stopped"]) is not bool:
            raise ValueError("invocation stopped must be boolean")
        result[identifier] = invocation
    return result


def _requests(value: Any, config: dict[str, Any], invocations: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("requests must be a list")
    attempts: set[str] = set()
    tickets: set[int] = set()
    result: list[dict[str, Any]] = []
    for request in value:
        if not isinstance(request, dict) or not _REQUEST_KEYS <= set(request) or set(request) - _REQUEST_KEYS - {"disk_mib", "exclusive"}:
            raise ValueError("request has an invalid schema")
        ticket = _integer(request["ticket"], "request ticket", minimum=0)
        attempt = _identifier(request["attempt"], "request attempt")
        invocation = _identifier(request["invocation"], "request invocation")
        if not isinstance(request["phase"], str) or request["phase"] not in {"waiting", "running"}:
            raise ValueError("request phase must be waiting or running")
        cpu = _integer(request["cpu_millis"], "request cpu_millis", minimum=1)
        memory = _integer(request["memory_mib"], "request memory_mib", minimum=1)
        if cpu > config["cpu_millis"] or memory > config["memory_mib"]:
            raise ValueError("request demand exceeds scheduler capacity")
        validate_demand({key: request[key] for key in ("cpu_millis", "memory_mib", "disk_mib", "exclusive") if key in request}, config)
        if invocation not in invocations:
            raise ValueError("request references an unknown invocation")
        if ticket in tickets or attempt in attempts:
            raise ValueError("request tickets and attempts must be unique")
        tickets.add(ticket)
        attempts.add(attempt)
        result.append(request)
    return result


def choose(config: Any, requests: Any, invocations: Any) -> str | None:
    """Return the single waiting attempt eligible for admission, if any.

    Running requests consume scheduler resources and their invocation's parallel
    cap.  FIFO never bypasses its oldest eligible waiting request.  Fair policy
    selects an invocation head by service turn, then refuses to backfill when
    that selected head cannot fit the remaining resources.
    """
    checked_config = validate_config(config)
    checked_invocations = _invocations(invocations, checked_config["max_running"])
    checked_requests = _requests(requests, checked_config, checked_invocations)

    running = [request for request in checked_requests if request["phase"] == "running"]
    if len(running) >= checked_config["max_running"]:
        return None
    used_cpu = sum(request["cpu_millis"] for request in running)
    used_memory = sum(request["memory_mib"] for request in running)
    used_disk = sum(request.get("disk_mib", 0) for request in running)
    occupied = {item for request in running for item in request.get("exclusive", [])}
    running_by_invocation = {
        identifier: sum(request["invocation"] == identifier for request in running)
        for identifier in checked_invocations
    }
    waiting = [
        request for request in checked_requests
        if request["phase"] == "waiting" and not checked_invocations[request["invocation"]]["stopped"]
    ]
    if not waiting:
        return None

    def fits(request: dict[str, Any]) -> bool:
        return (used_cpu + request["cpu_millis"] <= checked_config["cpu_millis"]
                and used_memory + request["memory_mib"] <= checked_config["memory_mib"]
                and used_disk + request.get("disk_mib", 0) <= checked_config.get("disk_mib", 0)
                and not occupied.intersection(request.get("exclusive", [])))

    if checked_config["policy"] == "fifo":
        candidate = min(waiting, key=lambda request: request["ticket"])
        if running_by_invocation[candidate["invocation"]] >= checked_invocations[candidate["invocation"]]["max_parallel"]:
            return None
        return candidate["attempt"] if fits(candidate) else None

    heads: list[dict[str, Any]] = []
    for identifier, invocation in checked_invocations.items():
        if invocation["stopped"] or running_by_invocation[identifier] >= invocation["max_parallel"]:
            continue
        candidates = [request for request in waiting if request["invocation"] == identifier]
        if candidates:
            heads.append(min(candidates, key=lambda request: request["ticket"]))
    if not heads:
        return None

    def fair_order(request: dict[str, Any]) -> tuple[bool, int, int]:
        turn = checked_invocations[request["invocation"]]["turn"]
        return (turn != 0, turn, request["ticket"])

    candidate = min(heads, key=fair_order)
    return candidate["attempt"] if fits(candidate) else None
