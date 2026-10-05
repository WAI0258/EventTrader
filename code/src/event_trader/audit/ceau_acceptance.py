"""Read-only CEAU acceptance summary derived from runtime logs."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from statistics import mean
from typing import Literal, cast

from event_trader.contracts import LLMUsageReceipt, RuntimeContractError
from event_trader.contracts._validators import validate_target_key
from event_trader.storage import WorkspaceLayout

CeauAcceptanceStatus = Literal["available", "unavailable"]


class CEAUAcceptanceSummaryError(ValueError):
    """Raised when a CEAU acceptance summary is malformed."""


@dataclass(frozen=True, slots=True)
class CEAUAcceptanceSummary:
    ceau_available: bool
    ceau_status: CeauAcceptanceStatus
    ceau_unavailable_reason: str | None
    routing_receipt_count: int
    route_count_by_route: dict[str, int]
    unit_opened_count: int
    unit_appended_count: int
    unit_emitted_count: int
    unit_event_count_distribution: dict[str, int]
    mirothinker_call_count: int
    events_per_mirothinker_call: float
    analysis_outcome_counts: dict[str, int]
    memory_updated_per_mirothinker_call: float
    no_update_per_mirothinker_call: float
    policy_version_distribution: dict[str, int]
    policy_hash_distribution: dict[str, int]
    units_emitted_by_processing_time_safety: int
    incomplete_live_unit_count: int
    absence_based_support_rejected_count: int | None
    llm_usage_total_tokens_by_agent_role: dict[str, int] = field(default_factory=dict)
    llm_usage_input_tokens_by_agent_role: dict[str, int] = field(default_factory=dict)
    llm_usage_output_tokens_by_agent_role: dict[str, int] = field(default_factory=dict)
    llm_usage_provider_reported_count_by_agent_role: dict[str, int] = field(
        default_factory=dict
    )
    llm_usage_unavailable_count_by_agent_role: dict[str, int] = field(
        default_factory=dict
    )
    analysis_tokens_per_mirothinker_call: float = 0.0
    analysis_tokens_per_emitted_unit: float = 0.0
    memory_updates_per_1k_analysis_tokens: float = 0.0

    def __post_init__(self) -> None:
        if self.ceau_status not in {"available", "unavailable"}:
            raise CEAUAcceptanceSummaryError(
                "ceau_status must be either available or unavailable."
            )
        if self.ceau_available and self.ceau_unavailable_reason is not None:
            raise CEAUAcceptanceSummaryError(
                "ceau_unavailable_reason must be null when ceau_available is true."
            )
        if (not self.ceau_available) and self.ceau_unavailable_reason is None:
            raise CEAUAcceptanceSummaryError(
                "ceau_unavailable_reason is required when ceau_status is unavailable."
            )
        for metric_name, metric_value in (
            ("routing_receipt_count", self.routing_receipt_count),
            ("unit_opened_count", self.unit_opened_count),
            ("unit_appended_count", self.unit_appended_count),
            ("unit_emitted_count", self.unit_emitted_count),
            ("mirothinker_call_count", self.mirothinker_call_count),
            (
                "units_emitted_by_processing_time_safety",
                self.units_emitted_by_processing_time_safety,
            ),
            ("incomplete_live_unit_count", self.incomplete_live_unit_count),
        ):
            if (
                not isinstance(metric_value, int)
                or isinstance(metric_value, bool)
                or metric_value < 0
            ):
                raise CEAUAcceptanceSummaryError(
                    f"{metric_name} must be a non-negative integer."
                )
        if self.events_per_mirothinker_call < 0:
            raise CEAUAcceptanceSummaryError(
                "events_per_mirothinker_call must be non-negative."
            )
        for metric_name, metric_value in (
            ("memory_updated_per_mirothinker_call", self.memory_updated_per_mirothinker_call),
            ("no_update_per_mirothinker_call", self.no_update_per_mirothinker_call),
        ):
            if (
                not isinstance(metric_value, int | float)
                or isinstance(metric_value, bool)
                or metric_value < 0
            ):
                raise CEAUAcceptanceSummaryError(f"{metric_name} must be non-negative.")
        for mapping_name, mapping_value in (
            ("route_count_by_route", self.route_count_by_route),
            ("unit_event_count_distribution", self.unit_event_count_distribution),
            ("analysis_outcome_counts", self.analysis_outcome_counts),
            ("policy_version_distribution", self.policy_version_distribution),
            ("policy_hash_distribution", self.policy_hash_distribution),
            ("llm_usage_total_tokens_by_agent_role", self.llm_usage_total_tokens_by_agent_role),
            ("llm_usage_input_tokens_by_agent_role", self.llm_usage_input_tokens_by_agent_role),
            ("llm_usage_output_tokens_by_agent_role", self.llm_usage_output_tokens_by_agent_role),
            (
                "llm_usage_provider_reported_count_by_agent_role",
                self.llm_usage_provider_reported_count_by_agent_role,
            ),
            (
                "llm_usage_unavailable_count_by_agent_role",
                self.llm_usage_unavailable_count_by_agent_role,
            ),
        ):
            if not isinstance(mapping_value, dict):
                raise CEAUAcceptanceSummaryError(
                    f"{mapping_name} must be a dict."
                )
            for metric_key, metric_value in mapping_value.items():
                if not isinstance(metric_key, str) or not metric_key.strip():
                    raise CEAUAcceptanceSummaryError(
                        f"{mapping_name} keys must be non-empty strings."
                    )
                if (
                    not isinstance(metric_value, int)
                    or isinstance(metric_value, bool)
                    or metric_value < 0
                ):
                    raise CEAUAcceptanceSummaryError(
                        f"{mapping_name} values must be non-negative integers."
                    )
        if self.absence_based_support_rejected_count is not None:
            if (
                not isinstance(self.absence_based_support_rejected_count, int)
                or isinstance(self.absence_based_support_rejected_count, bool)
                or self.absence_based_support_rejected_count < 0
            ):
                raise CEAUAcceptanceSummaryError(
                    "absence_based_support_rejected_count must be a non-negative integer."
                )
        for metric_name, metric_value in (
            ("analysis_tokens_per_mirothinker_call", self.analysis_tokens_per_mirothinker_call),
            ("analysis_tokens_per_emitted_unit", self.analysis_tokens_per_emitted_unit),
            ("memory_updates_per_1k_analysis_tokens", self.memory_updates_per_1k_analysis_tokens),
        ):
            if (
                not isinstance(metric_value, int | float)
                or isinstance(metric_value, bool)
                or metric_value < 0
            ):
                raise CEAUAcceptanceSummaryError(f"{metric_name} must be non-negative.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "ceau_available": self.ceau_available,
            "ceau_status": self.ceau_status,
            "ceau_unavailable_reason": self.ceau_unavailable_reason,
            "routing_receipt_count": self.routing_receipt_count,
            "route_count_by_route": dict(self.route_count_by_route),
            "unit_opened_count": self.unit_opened_count,
            "unit_appended_count": self.unit_appended_count,
            "unit_emitted_count": self.unit_emitted_count,
            "unit_event_count_distribution": dict(self.unit_event_count_distribution),
            "mirothinker_call_count": self.mirothinker_call_count,
            "events_per_mirothinker_call": self.events_per_mirothinker_call,
            "analysis_outcome_counts": dict(self.analysis_outcome_counts),
            "memory_updated_per_mirothinker_call": (
                self.memory_updated_per_mirothinker_call
            ),
            "no_update_per_mirothinker_call": self.no_update_per_mirothinker_call,
            "policy_version_distribution": dict(self.policy_version_distribution),
            "policy_hash_distribution": dict(self.policy_hash_distribution),
            "units_emitted_by_processing_time_safety": (
                self.units_emitted_by_processing_time_safety
            ),
            "incomplete_live_unit_count": self.incomplete_live_unit_count,
            "absence_based_support_rejected_count": (
                self.absence_based_support_rejected_count
            ),
            "llm_usage_total_tokens_by_agent_role": dict(
                self.llm_usage_total_tokens_by_agent_role
            ),
            "llm_usage_input_tokens_by_agent_role": dict(
                self.llm_usage_input_tokens_by_agent_role
            ),
            "llm_usage_output_tokens_by_agent_role": dict(
                self.llm_usage_output_tokens_by_agent_role
            ),
            "llm_usage_provider_reported_count_by_agent_role": dict(
                self.llm_usage_provider_reported_count_by_agent_role
            ),
            "llm_usage_unavailable_count_by_agent_role": dict(
                self.llm_usage_unavailable_count_by_agent_role
            ),
            "analysis_tokens_per_mirothinker_call": (
                self.analysis_tokens_per_mirothinker_call
            ),
            "analysis_tokens_per_emitted_unit": self.analysis_tokens_per_emitted_unit,
            "memory_updates_per_1k_analysis_tokens": (
                self.memory_updates_per_1k_analysis_tokens
            ),
        }


def build_ceau_acceptance_summary(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    run_id: str,
) -> CEAUAcceptanceSummary:
    """Build a read-only summary from runtime CEAU and analysis outcome logs."""
    if not isinstance(layout, WorkspaceLayout):
        raise CEAUAcceptanceSummaryError("layout must be a WorkspaceLayout instance.")
    normalized_target_key = validate_target_key(
        target_key,
        error_type=CEAUAcceptanceSummaryError,
    )
    normalized_run_id = _non_blank(run_id, "run_id")

    ceau_records = _read_ceau_records(
        layout=layout,
        target_key=normalized_target_key,
    )
    if not ceau_records:
        return _unavailable_summary(
            reason="no readable CEAU route/emission/runtime records were found.",
        )

    routing_receipt_count = 0
    route_count_by_route: dict[str, int] = {}
    unit_opened_count = 0
    unit_appended_count = 0
    unit_emitted_count = 0
    units_emitted_by_processing_time_safety = 0
    incomplete_live_unit_count = 0
    mirothinker_unit_ids: set[str] = set()
    unit_event_count_by_unit: dict[str, int] = {}
    unit_event_count_distribution: dict[str, int] = {}
    policy_version_distribution: dict[str, int] = {}
    policy_hash_distribution: dict[str, int] = {}

    for payload in ceau_records:
        record_type = _as_text(payload.get("record_type"))
        if record_type is None:
            continue

        if record_type == "event_route":
            routing_receipt_count += 1
            route = _as_text(payload.get("final_route"))
            if route is None:
                route = "unknown"
            route_count_by_route[route] = route_count_by_route.get(route, 0) + 1
            _collect_policy_distributions(
                payload=payload,
                policy_version_distribution=policy_version_distribution,
                policy_hash_distribution=policy_hash_distribution,
            )
            continue

        if record_type == "unit_opened":
            unit_opened_count += 1
            continue

        if record_type == "unit_appended":
            unit_appended_count += 1
            continue

        if record_type == "unit_emitted":
            unit_emitted_count += 1
            analysis_unit_id = _as_text(payload.get("analysis_unit_id"))
            event_count = _event_count(payload.get("event_ids"))
            if analysis_unit_id is not None:
                unit_event_count_by_unit[analysis_unit_id] = event_count
            bucket = str(event_count)
            unit_event_count_distribution[bucket] = (
                unit_event_count_distribution.get(bucket, 0) + 1
            )
            if (
                _as_text(payload.get("emission_clock")) == "processing_time"
                and _as_text(payload.get("emission_reason"))
                == "processing_time_safety_close"
            ):
                units_emitted_by_processing_time_safety += 1
            if payload.get("event_time_completeness_claim") is False:
                incomplete_live_unit_count += 1
            _collect_policy_distributions(
                payload=payload,
                policy_version_distribution=policy_version_distribution,
                policy_hash_distribution=policy_hash_distribution,
            )
            continue

        if record_type == "analysis_started":
            analysis_unit_id = _as_text(payload.get("analysis_unit_id"))
            if analysis_unit_id is not None:
                mirothinker_unit_ids.add(analysis_unit_id)
            continue

    outcome_records = _load_analysis_outcome_records(
        layout=layout,
        target_key=normalized_target_key,
        run_id=normalized_run_id,
    )
    checker_records = _load_checker_decision_records(
        layout=layout,
        target_key=normalized_target_key,
        run_id=normalized_run_id,
    )
    analysis_outcome_counts: dict[str, int] = {}
    for outcome_record in outcome_records:
        outcome = _as_text(outcome_record.get("outcome"))
        if outcome not in {"memory_updated", "no_update"}:
            continue
        analysis_outcome_counts[outcome] = analysis_outcome_counts.get(outcome, 0) + 1
    llm_usage_summary = _summarize_llm_usage(
        agent_records_by_role={
            "analysis": outcome_records,
            "checker": checker_records,
        }
    )
    analysis_total_tokens = llm_usage_summary.total_tokens_by_role.get("analysis", 0)

    if mirothinker_unit_ids:
        events_per_unit = [
            unit_event_count_by_unit.get(unit_id, 0) for unit_id in mirothinker_unit_ids
        ]
        events_per_mirothinker_call = (
            mean(events_per_unit) if events_per_unit else 0.0
        )
    else:
        events_per_mirothinker_call = 0.0

    return CEAUAcceptanceSummary(
        ceau_available=True,
        ceau_status="available",
        ceau_unavailable_reason=None,
        routing_receipt_count=routing_receipt_count,
        route_count_by_route=route_count_by_route,
        unit_opened_count=unit_opened_count,
        unit_appended_count=unit_appended_count,
        unit_emitted_count=unit_emitted_count,
        unit_event_count_distribution=unit_event_count_distribution,
        mirothinker_call_count=len(mirothinker_unit_ids),
        events_per_mirothinker_call=events_per_mirothinker_call,
        analysis_outcome_counts=analysis_outcome_counts,
        memory_updated_per_mirothinker_call=_per_call(
            analysis_outcome_counts.get("memory_updated", 0),
            len(mirothinker_unit_ids),
        ),
        no_update_per_mirothinker_call=_per_call(
            analysis_outcome_counts.get("no_update", 0),
            len(mirothinker_unit_ids),
        ),
        policy_version_distribution=policy_version_distribution,
        policy_hash_distribution=policy_hash_distribution,
        units_emitted_by_processing_time_safety=units_emitted_by_processing_time_safety,
        incomplete_live_unit_count=incomplete_live_unit_count,
        absence_based_support_rejected_count=_count_absence_rejected_records(
            outcome_records,
        ),
        llm_usage_total_tokens_by_agent_role=llm_usage_summary.total_tokens_by_role,
        llm_usage_input_tokens_by_agent_role=llm_usage_summary.input_tokens_by_role,
        llm_usage_output_tokens_by_agent_role=llm_usage_summary.output_tokens_by_role,
        llm_usage_provider_reported_count_by_agent_role=(
            llm_usage_summary.provider_reported_count_by_role
        ),
        llm_usage_unavailable_count_by_agent_role=(
            llm_usage_summary.unavailable_count_by_role
        ),
        analysis_tokens_per_mirothinker_call=_per_call(
            analysis_total_tokens,
            len(mirothinker_unit_ids),
        ),
        analysis_tokens_per_emitted_unit=_per_call(
            analysis_total_tokens,
            unit_emitted_count,
        ),
        memory_updates_per_1k_analysis_tokens=_per_1k_tokens(
            analysis_outcome_counts.get("memory_updated", 0),
            analysis_total_tokens,
        ),
    )


def parse_ceau_acceptance_summary(
    payload: Mapping[str, object] | None,
) -> CEAUAcceptanceSummary:
    if payload is None:
        return _unavailable_summary(
            reason="legacy report does not include CEAU acceptance summary.",
        )
    return CEAUAcceptanceSummary(
        ceau_available=_require_bool(payload.get("ceau_available"), "ceau_available"),
        ceau_status=_require_status(payload.get("ceau_status")),
        ceau_unavailable_reason=_optional_text(payload.get("ceau_unavailable_reason")),
        routing_receipt_count=_require_non_negative_int(
            payload.get("routing_receipt_count"),
            "routing_receipt_count",
        ),
        route_count_by_route=_require_int_map(
            payload.get("route_count_by_route"),
            "route_count_by_route",
        ),
        unit_opened_count=_require_non_negative_int(
            payload.get("unit_opened_count"),
            "unit_opened_count",
        ),
        unit_appended_count=_require_non_negative_int(
            payload.get("unit_appended_count"),
            "unit_appended_count",
        ),
        unit_emitted_count=_require_non_negative_int(
            payload.get("unit_emitted_count"),
            "unit_emitted_count",
        ),
        unit_event_count_distribution=_require_int_map(
            payload.get("unit_event_count_distribution"),
            "unit_event_count_distribution",
        ),
        mirothinker_call_count=_require_non_negative_int(
            payload.get("mirothinker_call_count"),
            "mirothinker_call_count",
        ),
        events_per_mirothinker_call=_require_float(
            payload.get("events_per_mirothinker_call"),
            "events_per_mirothinker_call",
        ),
        analysis_outcome_counts=_require_int_map(
            payload.get("analysis_outcome_counts"),
            "analysis_outcome_counts",
        ),
        memory_updated_per_mirothinker_call=_require_float(
            payload.get("memory_updated_per_mirothinker_call"),
            "memory_updated_per_mirothinker_call",
        ),
        no_update_per_mirothinker_call=_require_float(
            payload.get("no_update_per_mirothinker_call"),
            "no_update_per_mirothinker_call",
        ),
        policy_version_distribution=_require_int_map(
            payload.get("policy_version_distribution"),
            "policy_version_distribution",
        ),
        policy_hash_distribution=_require_int_map(
            payload.get("policy_hash_distribution"),
            "policy_hash_distribution",
        ),
        units_emitted_by_processing_time_safety=_require_non_negative_int(
            payload.get("units_emitted_by_processing_time_safety"),
            "units_emitted_by_processing_time_safety",
        ),
        incomplete_live_unit_count=_require_non_negative_int(
            payload.get("incomplete_live_unit_count"),
            "incomplete_live_unit_count",
        ),
        absence_based_support_rejected_count=_optional_non_negative_int(
            payload.get("absence_based_support_rejected_count")
        ),
        llm_usage_total_tokens_by_agent_role=_optional_int_map(
            payload.get("llm_usage_total_tokens_by_agent_role"),
            "llm_usage_total_tokens_by_agent_role",
        ),
        llm_usage_input_tokens_by_agent_role=_optional_int_map(
            payload.get("llm_usage_input_tokens_by_agent_role"),
            "llm_usage_input_tokens_by_agent_role",
        ),
        llm_usage_output_tokens_by_agent_role=_optional_int_map(
            payload.get("llm_usage_output_tokens_by_agent_role"),
            "llm_usage_output_tokens_by_agent_role",
        ),
        llm_usage_provider_reported_count_by_agent_role=_optional_int_map(
            payload.get("llm_usage_provider_reported_count_by_agent_role"),
            "llm_usage_provider_reported_count_by_agent_role",
        ),
        llm_usage_unavailable_count_by_agent_role=_optional_int_map(
            payload.get("llm_usage_unavailable_count_by_agent_role"),
            "llm_usage_unavailable_count_by_agent_role",
        ),
        analysis_tokens_per_mirothinker_call=_optional_float(
            payload.get("analysis_tokens_per_mirothinker_call"),
            "analysis_tokens_per_mirothinker_call",
        ),
        analysis_tokens_per_emitted_unit=_optional_float(
            payload.get("analysis_tokens_per_emitted_unit"),
            "analysis_tokens_per_emitted_unit",
        ),
        memory_updates_per_1k_analysis_tokens=_optional_float(
            payload.get("memory_updates_per_1k_analysis_tokens"),
            "memory_updates_per_1k_analysis_tokens",
        ),
    )


def render_ceau_acceptance_summary_markdown(
    summary: CEAUAcceptanceSummary,
) -> str:
    lines = [
        f"- CEAU Available: `{summary.ceau_available}`",
        f"- Status: `{summary.ceau_status}`",
        f"- Routing Receipt Count: `{summary.routing_receipt_count}`",
        f"- Unit Opened Count: `{summary.unit_opened_count}`",
        f"- Unit Appended Count: `{summary.unit_appended_count}`",
        f"- Unit Emitted Count: `{summary.unit_emitted_count}`",
        f"- Route Count By Route: `{json.dumps(summary.route_count_by_route, sort_keys=True)}`",
        (
            "- Unit Event Count Distribution: "
            f"`{json.dumps(summary.unit_event_count_distribution, sort_keys=True)}`"
        ),
        f"- MiroThinker Calls from Analysis Lifecycle Records: `{summary.mirothinker_call_count}`",
        f"- Events Per MiroThinker Call: `{summary.events_per_mirothinker_call}`",
        f"- Memory Updates Per MiroThinker Call: `{summary.memory_updated_per_mirothinker_call}`",
        f"- No-Update Outcomes Per MiroThinker Call: `{summary.no_update_per_mirothinker_call}`",
        (
            "- LLM Total Tokens By Agent Role: "
            f"`{json.dumps(summary.llm_usage_total_tokens_by_agent_role, sort_keys=True)}`"
        ),
        (
            "- LLM Usage Unavailable Count By Agent Role: "
            f"`{json.dumps(summary.llm_usage_unavailable_count_by_agent_role, sort_keys=True)}`"
        ),
        f"- Analysis Tokens Per MiroThinker Call: `{summary.analysis_tokens_per_mirothinker_call}`",
        f"- Analysis Tokens Per Emitted Unit: `{summary.analysis_tokens_per_emitted_unit}`",
        (
            "- Memory Updates Per 1k Analysis Tokens: "
            f"`{summary.memory_updates_per_1k_analysis_tokens}`"
        ),
        (
            "- Absence-Based Support Rejected Count: "
            f"`{summary.absence_based_support_rejected_count}`"
            if summary.absence_based_support_rejected_count is not None
            else "- Absence-Based Support Rejected Count: `unavailable`"
        ),
        (
            f"- CEAU Unavailable Reason: `{summary.ceau_unavailable_reason}`"
            if summary.ceau_unavailable_reason is not None
            else "- CEAU Unavailable Reason: `None`"
        ),
        (
            "- Analysis Outcome Counts: "
            f"`{json.dumps(summary.analysis_outcome_counts, sort_keys=True)}`"
        ),
        (
            f"- Policy Version Distribution: "
            f"`{json.dumps(summary.policy_version_distribution, sort_keys=True)}`"
        ),
        (
            f"- Policy Hash Distribution: "
            f"`{json.dumps(summary.policy_hash_distribution, sort_keys=True)}`"
        ),
        (
            "- Units Emitted By Processing-Time Safety: "
            f"`{summary.units_emitted_by_processing_time_safety}`"
        ),
        f"- Incomplete-Live Units: `{summary.incomplete_live_unit_count}`",
        "",
    ]
    return "\n".join(lines)


def _count_absence_rejected_records(
    outcome_records: Iterable[dict[str, object]],
) -> int | None:
    del outcome_records
    # Rejected absence/maturity writes fail before commit, so current persisted
    # analysis outcome diagnostics only prove accepted write payload usage.
    # Do not infer rejections from payload_count - used_count.
    return None


@dataclass(frozen=True, slots=True)
class _LLMUsageSummary:
    input_tokens_by_role: dict[str, int]
    output_tokens_by_role: dict[str, int]
    total_tokens_by_role: dict[str, int]
    provider_reported_count_by_role: dict[str, int]
    unavailable_count_by_role: dict[str, int]


def _summarize_llm_usage(
    *,
    agent_records_by_role: Mapping[str, Iterable[dict[str, object]]],
) -> _LLMUsageSummary:
    input_tokens_by_role: dict[str, int] = {}
    output_tokens_by_role: dict[str, int] = {}
    total_tokens_by_role: dict[str, int] = {}
    provider_reported_count_by_role: dict[str, int] = {}
    unavailable_count_by_role: dict[str, int] = {}
    for fallback_role, records in agent_records_by_role.items():
        for record in records:
            try:
                usage = LLMUsageReceipt.from_json_payload_optional(
                    record.get("llm_usage")
                )
            except RuntimeContractError:
                _increment(unavailable_count_by_role, fallback_role, 1)
                continue
            if usage is None:
                _increment(unavailable_count_by_role, fallback_role, 1)
                continue
            if usage.usage_source != "provider_reported":
                _increment(unavailable_count_by_role, usage.agent_role, 1)
                continue
            _increment(input_tokens_by_role, usage.agent_role, usage.input_tokens)
            _increment(output_tokens_by_role, usage.agent_role, usage.output_tokens)
            _increment(total_tokens_by_role, usage.agent_role, usage.total_tokens)
            _increment(provider_reported_count_by_role, usage.agent_role, 1)
    return _LLMUsageSummary(
        input_tokens_by_role=input_tokens_by_role,
        output_tokens_by_role=output_tokens_by_role,
        total_tokens_by_role=total_tokens_by_role,
        provider_reported_count_by_role=provider_reported_count_by_role,
        unavailable_count_by_role=unavailable_count_by_role,
    )


def _increment(mapping: dict[str, int], key: str, amount: int) -> None:
    mapping[key] = mapping.get(key, 0) + amount


def _read_ceau_records(
    *,
    layout: WorkspaceLayout,
    target_key: str,
) -> list[dict[str, object]]:
    ceau_root = layout.runtime_root / "ceau" / target_key
    if not ceau_root.exists():
        return []
    if not ceau_root.is_dir():
        raise CEAUAcceptanceSummaryError(f"CEAU root is not a directory: {ceau_root}")

    records: list[dict[str, object]] = []
    for path in sorted(ceau_root.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise CEAUAcceptanceSummaryError(
                f"failed to read CEAU record file: {path}"
            ) from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CEAUAcceptanceSummaryError(
                    f"invalid CEAU JSON line in {path}"
                ) from exc
            if not isinstance(payload, dict):
                continue
            if payload.get("target_key") != target_key:
                continue
            records.append(payload)
    return records


def _load_analysis_outcome_records(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    run_id: str,
) -> list[dict[str, object]]:
    outcome_root = layout.runtime_root / "analysis_outcomes" / target_key
    if not outcome_root.exists():
        return []
    if not outcome_root.is_dir():
        raise CEAUAcceptanceSummaryError(
            f"analysis_outcomes root is not a directory: {outcome_root}"
        )

    records: list[dict[str, object]] = []
    for path in sorted(outcome_root.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise CEAUAcceptanceSummaryError(
                f"failed to read analysis outcome records: {path}"
            ) from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CEAUAcceptanceSummaryError(
                    f"invalid analysis outcome record in {path}"
                ) from exc
            if not isinstance(payload, dict):
                raise CEAUAcceptanceSummaryError(
                    f"analysis outcome record in {path} is not an object."
                )
            if payload.get("context_packet_run_id") == run_id:
                records.append(payload)
    return records


def _load_checker_decision_records(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    run_id: str,
) -> list[dict[str, object]]:
    checker_root = layout.runtime_root / "checker_decisions" / target_key
    if not checker_root.exists():
        return []
    if not checker_root.is_dir():
        raise CEAUAcceptanceSummaryError(
            f"checker_decisions root is not a directory: {checker_root}"
        )

    records: list[dict[str, object]] = []
    for path in sorted(checker_root.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise CEAUAcceptanceSummaryError(
                f"failed to read checker decision records: {path}"
            ) from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CEAUAcceptanceSummaryError(
                    f"invalid checker decision record in {path}"
                ) from exc
            if not isinstance(payload, dict):
                raise CEAUAcceptanceSummaryError(
                    f"checker decision record in {path} is not an object."
                )
            if payload.get("context_packet_run_id") == run_id:
                records.append(payload)
    return records


def _collect_policy_distributions(
    *,
    payload: Mapping[str, object],
    policy_version_distribution: dict[str, int],
    policy_hash_distribution: dict[str, int],
) -> None:
    policy_version = _as_text(payload.get("policy_version"))
    if policy_version is not None:
        policy_version_distribution[policy_version] = (
            policy_version_distribution.get(policy_version, 0) + 1
        )
    policy_hash = _as_text(payload.get("policy_hash"))
    if policy_hash is not None:
        policy_hash_distribution[policy_hash] = (
            policy_hash_distribution.get(policy_hash, 0) + 1
        )


def _unavailable_summary(reason: str) -> CEAUAcceptanceSummary:
    return CEAUAcceptanceSummary(
        ceau_available=False,
        ceau_status="unavailable",
        ceau_unavailable_reason=reason,
        routing_receipt_count=0,
        route_count_by_route={},
        unit_opened_count=0,
        unit_appended_count=0,
        unit_emitted_count=0,
        unit_event_count_distribution={},
        mirothinker_call_count=0,
        events_per_mirothinker_call=0.0,
        analysis_outcome_counts={},
        memory_updated_per_mirothinker_call=0.0,
        no_update_per_mirothinker_call=0.0,
        policy_version_distribution={},
        policy_hash_distribution={},
        units_emitted_by_processing_time_safety=0,
        incomplete_live_unit_count=0,
        absence_based_support_rejected_count=None,
    )


def build_unavailable_ceau_acceptance_summary(reason: str) -> CEAUAcceptanceSummary:
    """Return a deterministic unavailable summary when CEAU truth cannot be read."""
    return _unavailable_summary(reason)


def _event_count(event_ids: object) -> int:
    if not isinstance(event_ids, list | tuple):
        return 0
    return sum(1 for event_id in event_ids if _as_text(event_id) is not None)


def _per_call(count: int, call_count: int) -> float:
    if call_count <= 0:
        return 0.0
    return count / call_count


def _per_1k_tokens(count: int, total_tokens: int) -> float:
    if total_tokens <= 0:
        return 0.0
    return count * 1000 / total_tokens


def _require_non_negative_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CEAUAcceptanceSummaryError(
            f"{field_name} must be a non-negative integer."
        )
    return value


def _optional_non_negative_int(value: object) -> int | None:
    if value is None:
        return None
    return _require_non_negative_int(value, "value")


def _require_float(value: object, field_name: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise CEAUAcceptanceSummaryError(
            f"{field_name} must be a number."
        )
    return float(value)


def _optional_float(value: object, field_name: str) -> float:
    if value is None:
        return 0.0
    return _require_float(value, field_name)


def _as_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _as_text(value)


def _non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CEAUAcceptanceSummaryError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise CEAUAcceptanceSummaryError(f"{field_name} must be a boolean.")
    return value


def _require_status(value: object) -> CeauAcceptanceStatus:
    if value not in {"available", "unavailable"}:
        raise CEAUAcceptanceSummaryError("ceau_status must be available or unavailable.")
    return cast(CeauAcceptanceStatus, value)


def _require_int_map(value: object, field_name: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise CEAUAcceptanceSummaryError(f"{field_name} must be a dict.")
    result: dict[str, int] = {}
    for key, item_value in value.items():
        if not isinstance(key, str) or not key.strip():
            raise CEAUAcceptanceSummaryError(
                f"{field_name} keys must be non-empty strings."
            )
        result[key] = _require_non_negative_int(item_value, f"{field_name}[{key}]")
    return result


def _optional_int_map(value: object, field_name: str) -> dict[str, int]:
    if value is None:
        return {}
    return _require_int_map(value, field_name)


__all__ = [
    "CEAUAcceptanceSummary",
    "CEAUAcceptanceSummaryError",
    "build_ceau_acceptance_summary",
    "build_unavailable_ceau_acceptance_summary",
    "parse_ceau_acceptance_summary",
    "render_ceau_acceptance_summary_markdown",
]
