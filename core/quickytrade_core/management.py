"""Immutable source, ownership, and trade-management intent contracts.

These values describe what the operator selected when the entry intent was
persisted.  They do not imply that exit automation is currently enabled.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

SOURCES = frozenset({"TRADINGVIEW", "MANUAL_UI"})
OWNERSHIPS = frozenset({"APP_OWNED"})
MANAGEMENT_MODES = frozenset({"APP_MANAGED", "ENTRY_ONLY"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_MANAGEMENT_POLICY_REQUIRED_KEYS = frozenset(
    {"policyId", "version", "takeProfitLevels", "stopLossPercent"}
)
_MANAGEMENT_POLICY_OPTIONAL_KEYS = frozenset({"stopCoveragePercent", "transitions"})
_TRANSITION_ACTIONS = frozenset({"MOVE_STOP_TO_BREAKEVEN", "TRAIL_FRESH_BID"})


class ManagementContractError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def parse_management_contract(request: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical, persistence-safe management contract.

    The lower-case ``tradingview`` source and omitted management fields are a
    compatibility path for already-deployed capture-only callers.  They map to
    ``ENTRY_ONLY``; callers must explicitly send ``APP_MANAGED`` and a complete
    policy before the app may claim management ownership.
    """

    raw_source = request.get("source")
    if raw_source == "tradingview":
        source = "TRADINGVIEW"
    elif raw_source in SOURCES:
        source = raw_source
    else:
        raise ManagementContractError(
            "INVALID_REQUEST_SOURCE",
            "source must be TRADINGVIEW or MANUAL_UI",
        )

    ownership = request.get("ownership", "APP_OWNED")
    if ownership not in OWNERSHIPS:
        raise ManagementContractError(
            "INVALID_TRADE_OWNERSHIP",
            "Only APP_OWNED intents may enter the broker submission core",
        )

    mode = request.get("managementMode", "ENTRY_ONLY")
    if mode not in MANAGEMENT_MODES:
        raise ManagementContractError(
            "INVALID_MANAGEMENT_MODE",
            "managementMode must be APP_MANAGED or ENTRY_ONLY",
        )

    raw_policy = request.get("managementPolicy")
    if mode == "APP_MANAGED":
        if raw_policy is None:
            raise ManagementContractError(
                "MANAGEMENT_POLICY_REQUIRED",
                "APP_MANAGED requires an explicit versioned management policy",
            )
        policy = validate_management_policy(raw_policy)
    else:
        if raw_policy is not None:
            raise ManagementContractError(
                "MANAGEMENT_POLICY_NOT_APPLICABLE",
                "ENTRY_ONLY may not carry an app exit-management policy",
            )
        policy = None

    if source == "MANUAL_UI" and (
        "ownership" not in request or "managementMode" not in request
    ):
        raise ManagementContractError(
            "MANUAL_MANAGEMENT_SELECTION_REQUIRED",
            "Manual intents require explicit ownership and managementMode",
        )

    return {
        "source": source,
        "ownership": ownership,
        "managementMode": mode,
        "managementPolicy": policy,
    }


def validate_management_policy(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not _MANAGEMENT_POLICY_REQUIRED_KEYS.issubset(value)
        or not set(value) <= (_MANAGEMENT_POLICY_REQUIRED_KEYS | _MANAGEMENT_POLICY_OPTIONAL_KEYS)
    ):
        raise ManagementContractError(
            "MANAGEMENT_POLICY_INVALID",
            "Management policy fields must be policyId, version, takeProfitLevels, and stopLossPercent, "
            "with optional stopCoveragePercent and transitions",
        )

    policy_id = value["policyId"]
    if not isinstance(policy_id, str) or not _IDENTIFIER.fullmatch(policy_id):
        raise ManagementContractError(
            "MANAGEMENT_POLICY_INVALID", "policyId must be a stable identifier"
        )
    version = value["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ManagementContractError(
            "MANAGEMENT_POLICY_INVALID", "Management policy version must be a positive integer"
        )

    raw_levels = value["takeProfitLevels"]
    if not isinstance(raw_levels, list) or not 1 <= len(raw_levels) <= 8:
        raise ManagementContractError(
            "MANAGEMENT_POLICY_INVALID", "A management policy requires one to eight take-profit levels"
        )

    levels: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    previous_trigger = Decimal("0")
    allocation_total = Decimal("0")
    for raw_level in raw_levels:
        if not isinstance(raw_level, dict) or set(raw_level) != {
            "levelId",
            "triggerPercent",
            "allocationPercent",
        }:
            raise ManagementContractError(
                "MANAGEMENT_POLICY_INVALID",
                "Each take-profit level requires levelId, triggerPercent, and allocationPercent",
            )
        level_id = raw_level["levelId"]
        if (
            not isinstance(level_id, str)
            or not _IDENTIFIER.fullmatch(level_id)
            or level_id in seen_ids
        ):
            raise ManagementContractError(
                "MANAGEMENT_POLICY_INVALID", "Take-profit level identifiers must be unique"
            )
        trigger = _percent(raw_level["triggerPercent"], "triggerPercent", maximum=Decimal("1000"))
        allocation = _percent(
            raw_level["allocationPercent"], "allocationPercent", maximum=Decimal("100")
        )
        if trigger <= previous_trigger:
            raise ManagementContractError(
                "MANAGEMENT_POLICY_INVALID", "Take-profit triggers must be strictly increasing"
            )
        previous_trigger = trigger
        allocation_total += allocation
        seen_ids.add(level_id)
        levels.append(
            {
                "levelId": level_id,
                "triggerPercent": _decimal_text(trigger),
                "allocationPercent": _decimal_text(allocation),
            }
        )

    if allocation_total != Decimal("100"):
        raise ManagementContractError(
            "MANAGEMENT_ALLOCATION_INVALID",
            "Take-profit allocation percentages must total exactly 100",
        )

    stop_loss = _percent(value["stopLossPercent"], "stopLossPercent", maximum=Decimal("100"))
    result: dict[str, Any] = {
        "policyId": policy_id,
        "version": version,
        "takeProfitLevels": levels,
        "stopLossPercent": _decimal_text(stop_loss),
    }

    if "stopCoveragePercent" in value:
        # Parsed, validated, stored, and wired all the way from the operator UI
        # through server.mjs to here -- and then read by nothing in engine.py.
        # Every stop leg covers its level's full allocated quantity regardless
        # of what this says. Rather than continue to accept a risk setting that
        # silently does nothing, reject it: an operator who sets stop coverage
        # to 50% and gets 100% coverage has been told something untrue about
        # their own risk. Remove this rejection when the engine actually
        # implements partial stop coverage.
        coverage = _percent(value["stopCoveragePercent"], "stopCoveragePercent", maximum=Decimal("100"))
        if coverage != Decimal("100"):
            raise ManagementContractError(
                "STOP_COVERAGE_UNSUPPORTED",
                "stopCoveragePercent other than 100 is not implemented; every stop leg covers its "
                "level's full allocated quantity",
            )
        result["stopCoveragePercent"] = _decimal_text(coverage)

    if "transitions" in value:
        # Transitions reference take-profit levels by their bare levelId (e.g.
        # "TP1"), not the store's internal "<levelId>_FILLED" event name -- that
        # suffix is stripped at the Node wire boundary (server.mjs's
        # coreManagementPolicy) before the request reaches the core.
        result["transitions"] = _validate_transitions(value["transitions"], seen_ids)

    return result


def _validate_transitions(raw_transitions: Any, level_ids: set[str]) -> list[dict[str, str]]:
    if not isinstance(raw_transitions, list):
        raise ManagementContractError("MANAGEMENT_POLICY_INVALID", "transitions must be a list")

    transitions: list[dict[str, str]] = []
    for raw_transition in raw_transitions:
        if not isinstance(raw_transition, dict):
            raise ManagementContractError("MANAGEMENT_POLICY_INVALID", "Each transition must be an object")

        action = raw_transition.get("action")
        if action not in _TRANSITION_ACTIONS:
            raise ManagementContractError(
                "MANAGEMENT_POLICY_INVALID",
                "Transition action must be MOVE_STOP_TO_BREAKEVEN or TRAIL_FRESH_BID",
            )

        after = raw_transition.get("after")
        if not isinstance(after, str) or after not in level_ids:
            raise ManagementContractError(
                "MANAGEMENT_POLICY_INVALID",
                "Transition 'after' must reference an existing take-profit level id",
            )

        if action == "TRAIL_FRESH_BID":
            if set(raw_transition) != {"after", "action", "distancePercent"}:
                raise ManagementContractError(
                    "MANAGEMENT_POLICY_INVALID",
                    "TRAIL_FRESH_BID transitions require after, action, and distancePercent",
                )
            distance = _percent(raw_transition["distancePercent"], "distancePercent", maximum=Decimal("1000"))
            transitions.append({"after": after, "action": action, "distancePercent": _decimal_text(distance)})
        else:
            if set(raw_transition) != {"after", "action"}:
                raise ManagementContractError(
                    "MANAGEMENT_POLICY_INVALID",
                    "MOVE_STOP_TO_BREAKEVEN transitions must not include distancePercent or other fields",
                )
            transitions.append({"after": after, "action": action})

    return transitions


def _percent(value: Any, field: str, *, maximum: Decimal) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ManagementContractError(
            "MANAGEMENT_POLICY_INVALID", f"{field} must be a positive percentage"
        )
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ManagementContractError(
            "MANAGEMENT_POLICY_INVALID", f"{field} must be a positive percentage"
        ) from None
    if not result.is_finite() or result <= 0 or result > maximum:
        raise ManagementContractError(
            "MANAGEMENT_POLICY_INVALID", f"{field} is outside the supported range"
        )
    if result.as_tuple().exponent < -4:
        raise ManagementContractError(
            "MANAGEMENT_POLICY_INVALID", f"{field} supports at most four decimal places"
        )
    return result


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
