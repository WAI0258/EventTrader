"""Deterministic market-context usage facts for reflection."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from event_trader.contracts import ViewStateChange
from event_trader.validation.portfolio_feedback import PortfolioFeedbackSnapshot
from event_trader.validation.returns import (
    OpenEpisodeTerminalMark,
    ValidationReturnsResult,
)

from .contracts import ReflectionContractError

type MarketContextReceiptReader = Callable[
    [str, tuple[str, ...]],
    tuple[Mapping[str, object], ...],
]

_ANALYSIS_OUTCOME_DIR_NAME = "analysis_outcomes"
_CHECKER_DECISION_DIR_NAME = "checker_decisions"
_COMPONENT_STATUS_KEYS: Mapping[str, str] = {
    "price_volume": "price_volume_status",
    "technical": "technical_status",
    "derivatives": "derivatives_status",
    "macro_cross_asset": "macro_cross_asset_status",
}
_COMPONENT_TOKENS: Mapping[str, tuple[str, ...]] = {
    "price_volume": ("volume", "vwap", "price-volume", "price volume"),
    "technical": ("ema", "rsi", "bollinger", "ichimoku"),
    "derivatives": ("option", "options", "derivatives", "implied volatility", "greeks"),
    "macro_cross_asset": (
        "macro",
        "cross-asset",
        "cross asset",
        "dxy",
        "rates",
        "rate",
        "usd",
    ),
}


@dataclass(frozen=True, slots=True)
class MarketContextToolCallFacts:
    """Compact analysis market-terminal call facts persisted for reflection."""

    operation: str
    arguments: dict[str, object]
    status: str | None
    row_count: int | None
    contract_count: int | None
    payload_sha256: str | None
    truncated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.operation, str) or not self.operation.strip():
            raise ReflectionContractError("operation must be a non-blank string.")
        if not isinstance(self.arguments, dict):
            raise ReflectionContractError("arguments must be a dict.")
        for field_name in ("status", "payload_sha256"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ReflectionContractError(
                    f"{field_name} must be a non-blank string when present."
                )
        for field_name in ("row_count", "contract_count"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ReflectionContractError(
                    f"{field_name} must be a non-negative integer when present."
                )
        if not isinstance(self.truncated, bool):
            raise ReflectionContractError("truncated must be a boolean.")

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "arguments": dict(self.arguments),
            "status": self.status,
            "row_count": self.row_count,
            "contract_count": self.contract_count,
            "payload_sha256": self.payload_sha256,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class MarketContextDecisionUsageFacts:
    """One view-state decision's decision-time market-context usage facts."""

    state_change_id: str
    effective_at: datetime
    context_visible: bool
    market_context_hash: str | None
    component_statuses: dict[str, str]
    calculation_versions: dict[str, str]
    available_components: tuple[str, ...]
    partial_or_unavailable_components: tuple[str, ...]
    source_receipt_type: str
    source_event_ids: tuple[str, ...]
    decision_text_reference_status: str
    referenced_components: tuple[str, ...]
    unreferenced_available_components: tuple[str, ...]
    market_tool_calls: tuple[MarketContextToolCallFacts, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state_change_id, str) or not self.state_change_id.strip():
            raise ReflectionContractError("state_change_id must be a non-blank string.")
        if not isinstance(self.effective_at, datetime):
            raise ReflectionContractError("effective_at must be a datetime.")
        if not isinstance(self.context_visible, bool):
            raise ReflectionContractError("context_visible must be a boolean.")
        if self.market_context_hash is not None and (
            not isinstance(self.market_context_hash, str)
            or not self.market_context_hash.strip()
        ):
            raise ReflectionContractError(
                "market_context_hash must be a non-blank string when present."
            )
        _validate_str_dict(self.component_statuses, "component_statuses")
        _validate_str_dict(self.calculation_versions, "calculation_versions")
        for field_name in (
            "available_components",
            "partial_or_unavailable_components",
            "source_event_ids",
            "referenced_components",
            "unreferenced_available_components",
            "warnings",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, tuple) or not all(
                isinstance(item, str) for item in value
            ):
                raise ReflectionContractError(f"{field_name} must be a tuple of strings.")
        if self.source_receipt_type not in {
            "analysis_outcome",
            "checker_receipt",
            "missing",
        }:
            raise ReflectionContractError("source_receipt_type is invalid.")
        if self.decision_text_reference_status not in {
            "referenced_by_text",
            "not_referenced_by_text",
            "no_decision_text",
        }:
            raise ReflectionContractError("decision_text_reference_status is invalid.")
        if not isinstance(self.market_tool_calls, tuple) or not all(
            isinstance(item, MarketContextToolCallFacts)
            for item in self.market_tool_calls
        ):
            raise ReflectionContractError(
                "market_tool_calls must be a tuple of MarketContextToolCallFacts."
            )

    @property
    def market_tool_call_count(self) -> int:
        return len(self.market_tool_calls)

    @property
    def market_tool_statuses(self) -> tuple[str, ...]:
        return tuple(
            call.status
            for call in self.market_tool_calls
            if call.status is not None
        )


@dataclass(frozen=True, slots=True)
class MarketContextUsageFacts:
    """Reflection-owned container for per-decision usage facts."""

    context_visible: bool
    source_event_ids: tuple[str, ...]
    decision_usages: tuple[MarketContextDecisionUsageFacts, ...]
    post_decision_outcome_summary: dict[str, object]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.context_visible, bool):
            raise ReflectionContractError("context_visible must be a boolean.")
        if not isinstance(self.source_event_ids, tuple) or not all(
            isinstance(item, str) for item in self.source_event_ids
        ):
            raise ReflectionContractError("source_event_ids must be a tuple of strings.")
        if not isinstance(self.decision_usages, tuple) or not all(
            isinstance(item, MarketContextDecisionUsageFacts)
            for item in self.decision_usages
        ):
            raise ReflectionContractError(
                "decision_usages must be a tuple of MarketContextDecisionUsageFacts."
            )
        if not isinstance(self.post_decision_outcome_summary, dict):
            raise ReflectionContractError(
                "post_decision_outcome_summary must be a dict."
            )
        if not isinstance(self.warnings, tuple) or not all(
            isinstance(item, str) for item in self.warnings
        ):
            raise ReflectionContractError("warnings must be a tuple of strings.")


def build_market_context_usage_facts(
    *,
    target_key: str,
    state_changes: tuple[ViewStateChange, ...],
    read_analysis_outcomes: MarketContextReceiptReader,
    read_checker_receipts: MarketContextReceiptReader,
    market_returns: ValidationReturnsResult | None = None,
    terminal_mark: OpenEpisodeTerminalMark | None = None,
    portfolio_feedback: PortfolioFeedbackSnapshot | None = None,
) -> MarketContextUsageFacts:
    """Build usage facts from persisted decision receipts only."""
    decision_usages = tuple(
        _build_decision_usage(
            target_key=target_key,
            state_change=state_change,
            read_analysis_outcomes=read_analysis_outcomes,
            read_checker_receipts=read_checker_receipts,
        )
        for state_change in state_changes
    )
    source_event_ids = _source_event_ids_first_seen(state_changes)
    warnings = tuple(
        warning
        for usage in decision_usages
        for warning in usage.warnings
    )
    return MarketContextUsageFacts(
        context_visible=any(usage.context_visible for usage in decision_usages),
        source_event_ids=source_event_ids,
        decision_usages=decision_usages,
        post_decision_outcome_summary=_post_decision_outcome_summary(
            market_returns=market_returns,
            terminal_mark=terminal_mark,
            portfolio_feedback=portfolio_feedback,
        ),
        warnings=warnings,
    )


def _build_decision_usage(
    *,
    target_key: str,
    state_change: ViewStateChange,
    read_analysis_outcomes: MarketContextReceiptReader,
    read_checker_receipts: MarketContextReceiptReader,
) -> MarketContextDecisionUsageFacts:
    source_event_ids = state_change.source_event_ids
    analysis_outcomes = read_analysis_outcomes(target_key, source_event_ids)
    selected, warnings = _select_latest_receipt(
        _exact_analysis_matches(analysis_outcomes, source_event_ids),
        timestamp_field="committed_at",
        duplicate_warning="duplicate_analysis_outcomes_latest_wins",
    )
    source_receipt_type = "analysis_outcome"
    if selected is None:
        checker_receipts = read_checker_receipts(target_key, source_event_ids)
        selected, warnings = _select_latest_receipt(
            _checker_matches(checker_receipts, source_event_ids),
            timestamp_field="recorded_at",
            duplicate_warning="duplicate_checker_receipts_latest_wins",
        )
        source_receipt_type = "checker_receipt"

    if selected is None:
        return _missing_decision_usage(state_change)

    audit = selected.get("market_context_audit")
    market_tool_calls = _market_tool_calls(selected.get("market_tool_usage"))
    source_ids = _receipt_event_ids(selected, fallback=source_event_ids)
    text = _decision_text(selected)
    referenced_components = _referenced_components(text)
    decision_text_reference_status = (
        "no_decision_text"
        if not text.strip()
        else (
            "referenced_by_text"
            if referenced_components
            else "not_referenced_by_text"
        )
    )
    if not isinstance(audit, Mapping):
        return MarketContextDecisionUsageFacts(
            state_change_id=state_change.state_change_id,
            effective_at=state_change.effective_at,
            context_visible=False,
            market_context_hash=None,
            component_statuses={},
            calculation_versions={},
            available_components=(),
            partial_or_unavailable_components=(),
            source_receipt_type=source_receipt_type,
            source_event_ids=source_ids,
            decision_text_reference_status=decision_text_reference_status,
            referenced_components=referenced_components,
            unreferenced_available_components=(),
            market_tool_calls=market_tool_calls,
            warnings=warnings,
        )

    component_statuses = _component_statuses(audit)
    available_components = tuple(
        component
        for component, status in sorted(component_statuses.items())
        if status == "available"
    )
    partial_or_unavailable_components = tuple(
        component
        for component, status in sorted(component_statuses.items())
        if status != "available"
    )
    unreferenced_available_components = tuple(
        component
        for component in available_components
        if component not in referenced_components
    )
    return MarketContextDecisionUsageFacts(
        state_change_id=state_change.state_change_id,
        effective_at=state_change.effective_at,
        context_visible=True,
        market_context_hash=_optional_text(audit.get("market_context_hash")),
        component_statuses=component_statuses,
        calculation_versions=_str_dict(audit.get("calculation_versions")),
        available_components=available_components,
        partial_or_unavailable_components=partial_or_unavailable_components,
        source_receipt_type=source_receipt_type,
        source_event_ids=source_ids,
        decision_text_reference_status=decision_text_reference_status,
        referenced_components=referenced_components,
        unreferenced_available_components=unreferenced_available_components,
        market_tool_calls=market_tool_calls,
        warnings=warnings,
    )


def read_analysis_outcome_receipts(
    runtime_root: Path,
    target_key: str,
    source_event_ids: tuple[str, ...],
) -> tuple[Mapping[str, object], ...]:
    return _read_receipts(
        runtime_root / _ANALYSIS_OUTCOME_DIR_NAME / target_key,
        event_id_fields=("event_ids",),
        source_event_ids=source_event_ids,
    )


def read_checker_decision_receipts(
    runtime_root: Path,
    target_key: str,
    source_event_ids: tuple[str, ...],
) -> tuple[Mapping[str, object], ...]:
    return _read_receipts(
        runtime_root / _CHECKER_DECISION_DIR_NAME / target_key,
        event_id_fields=("event_id",),
        source_event_ids=source_event_ids,
    )


def empty_market_context_receipt_reader(
    _target_key: str,
    _source_event_ids: tuple[str, ...],
) -> tuple[Mapping[str, object], ...]:
    return ()


def _select_latest_receipt(
    receipts: tuple[Mapping[str, object], ...],
    *,
    timestamp_field: str,
    duplicate_warning: str,
) -> tuple[Mapping[str, object] | None, tuple[str, ...]]:
    if not receipts:
        return None, ()
    indexed = tuple(enumerate(receipts))
    selected_index, selected = max(
        indexed,
        key=lambda item: _receipt_sort_key(item[0], item[1], timestamp_field),
    )
    warnings = (duplicate_warning,) if len(receipts) > 1 else ()
    return selected, warnings


def _missing_decision_usage(
    state_change: ViewStateChange,
) -> MarketContextDecisionUsageFacts:
    return MarketContextDecisionUsageFacts(
        state_change_id=state_change.state_change_id,
        effective_at=state_change.effective_at,
        context_visible=False,
        market_context_hash=None,
        component_statuses={},
        calculation_versions={},
        available_components=(),
        partial_or_unavailable_components=(),
        source_receipt_type="missing",
        source_event_ids=state_change.source_event_ids,
        decision_text_reference_status="no_decision_text",
        referenced_components=(),
        unreferenced_available_components=(),
        market_tool_calls=(),
    )


def _exact_analysis_matches(
    receipts: tuple[Mapping[str, object], ...],
    source_event_ids: tuple[str, ...],
) -> tuple[Mapping[str, object], ...]:
    source_key = tuple(source_event_ids)
    return tuple(
        receipt
        for receipt in receipts
        if _receipt_event_ids(receipt, fallback=()) == source_key
    )


def _checker_matches(
    receipts: tuple[Mapping[str, object], ...],
    source_event_ids: tuple[str, ...],
) -> tuple[Mapping[str, object], ...]:
    source_event_id_set = set(source_event_ids)
    return tuple(
        receipt
        for receipt in receipts
        if source_event_id_set.intersection(_receipt_event_ids(receipt, fallback=()))
    )


def _receipt_sort_key(
    index: int,
    receipt: Mapping[str, object],
    timestamp_field: str,
) -> tuple[int, datetime, int]:
    timestamp = _parse_datetime(receipt.get(timestamp_field))
    if timestamp is None:
        return (0, datetime.min.replace(tzinfo=UTC), index)
    return (1, timestamp, index)


def _component_statuses(audit: Mapping[str, object]) -> dict[str, str]:
    raw_component_statuses = audit.get("component_statuses")
    if isinstance(raw_component_statuses, Mapping):
        return _str_dict(raw_component_statuses)
    statuses: dict[str, str] = {}
    for component, key in _COMPONENT_STATUS_KEYS.items():
        value = audit.get(key)
        if isinstance(value, str) and value.strip():
            statuses[component] = value.strip()
    return dict(sorted(statuses.items()))


def _decision_text(receipt: Mapping[str, object]) -> str:
    parts: list[str] = []
    for field_name in ("rationale", "attention_hint", "why_escalated"):
        value = receipt.get(field_name)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    view_state_change = receipt.get("view_state_change")
    if isinstance(view_state_change, Mapping):
        rationale = view_state_change.get("rationale_md")
        if isinstance(rationale, str) and rationale.strip():
            parts.append(rationale)
    return "\n".join(parts)


def _referenced_components(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(
        component
        for component, tokens in sorted(_COMPONENT_TOKENS.items())
        if any(token in lowered for token in tokens)
    )


def _market_tool_calls(value: object) -> tuple[MarketContextToolCallFacts, ...]:
    if not isinstance(value, Mapping):
        return ()
    calls = value.get("calls")
    if not isinstance(calls, list):
        return ()
    normalized: list[MarketContextToolCallFacts] = []
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        operation = _optional_text(call.get("operation"))
        if operation is None:
            continue
        arguments = call.get("arguments")
        normalized.append(
            MarketContextToolCallFacts(
                operation=operation,
                arguments=(
                    _object_dict(arguments)
                    if isinstance(arguments, Mapping)
                    else {}
                ),
                status=_optional_text(call.get("status")),
                row_count=_optional_non_negative_int(call.get("row_count")),
                contract_count=_optional_non_negative_int(call.get("contract_count")),
                payload_sha256=_optional_text(call.get("payload_sha256")),
                truncated=call.get("truncated") is True,
            )
        )
    return tuple(normalized)


def _object_dict(value: Mapping[str, object]) -> dict[str, object]:
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str) and _is_json_scalar_or_container(item)
    }


def _is_json_scalar_or_container(value: object) -> bool:
    if value is None or isinstance(value, str | int | float | bool):
        return True
    if isinstance(value, list):
        return all(_is_json_scalar_or_container(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_scalar_or_container(item)
            for key, item in value.items()
        )
    return False


def _optional_non_negative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _post_decision_outcome_summary(
    *,
    market_returns: ValidationReturnsResult | None,
    terminal_mark: OpenEpisodeTerminalMark | None,
    portfolio_feedback: PortfolioFeedbackSnapshot | None,
) -> dict[str, object]:
    summary: dict[str, object] = {}
    if market_returns is not None:
        episode_return = (
            market_returns.episode_returns[0]
            if market_returns.episode_returns
            else None
        )
        summary["market_returns"] = {
            "episode_underlying_return": (
                None if episode_return is None else episode_return.underlying_return
            ),
            "episode_strategy_return": (
                None if episode_return is None else episode_return.strategy_return
            ),
            "segment_count": len(market_returns.segment_returns),
            "horizon_count": len(market_returns.horizon_baselines),
        }
    if terminal_mark is not None:
        summary["terminal_mark"] = {
            "underlying_return": terminal_mark.underlying_return,
            "strategy_return": terminal_mark.strategy_return,
            "target_weight": terminal_mark.target_weight,
            "replay_end_at": terminal_mark.replay_end_at.isoformat(),
        }
    if portfolio_feedback is not None:
        summary["portfolio_feedback"] = {
            "window_start": portfolio_feedback.window_start.isoformat(),
            "window_end": portfolio_feedback.window_end.isoformat(),
            "portfolio_facts": portfolio_feedback.portfolio_facts.to_dict(),
            "segment_count": len(portfolio_feedback.segment_facts),
            "decision_count": len(portfolio_feedback.decision_facts),
        }
    return summary


def _receipt_event_ids(
    receipt: Mapping[str, object],
    *,
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    view_state_change = receipt.get("view_state_change")
    if isinstance(view_state_change, Mapping):
        view_source_event_ids = view_state_change.get("source_event_ids")
        if isinstance(view_source_event_ids, list):
            normalized = tuple(
                event_id.strip()
                for event_id in view_source_event_ids
                if isinstance(event_id, str) and event_id.strip()
            )
            if normalized:
                return normalized
    event_ids = receipt.get("event_ids")
    if isinstance(event_ids, list):
        normalized = tuple(
            event_id.strip()
            for event_id in event_ids
            if isinstance(event_id, str) and event_id.strip()
        )
        if normalized:
            return normalized
    event_id = receipt.get("event_id")
    if isinstance(event_id, str) and event_id.strip():
        return (event_id.strip(),)
    return fallback


def _source_event_ids_first_seen(
    state_changes: tuple[ViewStateChange, ...],
) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for state_change in state_changes:
        for event_id in state_change.source_event_ids:
            if event_id in seen:
                continue
            seen.add(event_id)
            ordered.append(event_id)
    return tuple(ordered)


def _read_receipts(
    root: Path,
    *,
    event_id_fields: tuple[str, ...],
    source_event_ids: tuple[str, ...],
) -> tuple[Mapping[str, object], ...]:
    if not root.exists() or not root.is_dir():
        return ()
    source_event_id_set = set(source_event_ids)
    receipts: list[Mapping[str, object]] = []
    for path in sorted(root.glob("*.jsonl")):
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                payload = _load_json_object(line)
                if payload is None:
                    continue
                if not source_event_id_set.intersection(
                    _event_ids_from_payload(payload, event_id_fields)
                ):
                    continue
                receipts.append(payload)
    return tuple(receipts)


def _event_ids_from_payload(
    payload: Mapping[str, object],
    event_id_fields: tuple[str, ...],
) -> tuple[str, ...]:
    event_ids: list[str] = []
    for field_name in event_id_fields:
        value = payload.get(field_name)
        if isinstance(value, str) and value.strip():
            event_ids.append(value.strip())
        elif isinstance(value, list):
            event_ids.extend(
                item.strip()
                for item in value
                if isinstance(item, str) and item.strip()
            )
    return tuple(event_ids)


def _load_json_object(line: str) -> dict[str, object] | None:
    normalized = line.strip()
    if not normalized:
        return None
    try:
        payload: Any = json.loads(normalized)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _str_dict(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return dict(
        sorted(
            (str(key), str(item))
            for key, item in value.items()
            if isinstance(key, str) and isinstance(item, str)
        )
    )


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _validate_str_dict(value: dict[str, str], field_name: str) -> None:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        raise ReflectionContractError(f"{field_name} must be a string dict.")


__all__ = [
    "MarketContextDecisionUsageFacts",
    "MarketContextReceiptReader",
    "MarketContextToolCallFacts",
    "MarketContextUsageFacts",
    "build_market_context_usage_facts",
    "empty_market_context_receipt_reader",
    "read_analysis_outcome_receipts",
    "read_checker_decision_receipts",
]
