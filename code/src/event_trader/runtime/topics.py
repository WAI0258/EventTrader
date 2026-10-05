"""Central topic derivation helpers for the Nautilus-aligned runtime."""

from __future__ import annotations

from event_trader.contracts._validators import validate_page_key, validate_target_key


class RuntimeTopicError(ValueError):
    """Raised when a runtime topic or segment is invalid."""


def news_raw_topic(target_key: str) -> str:
    return f"data.news.raw.{_validate_target_key(target_key)}"


def web_result_raw_topic(target_key: str) -> str:
    return f"data.web_result.raw.{_validate_target_key(target_key)}"


def evidence_admitted_topic(target_key: str) -> str:
    return f"events.evidence.admitted.{_validate_target_key(target_key)}"


def evidence_quarantined_topic(target_key: str) -> str:
    return f"events.evidence.quarantined.{_validate_target_key(target_key)}"


def checker_requested_topic(target_key: str) -> str:
    return f"commands.checker.requested.{_validate_target_key(target_key)}"


def checker_decision_topic(target_key: str) -> str:
    return f"events.checker.decision.{_validate_target_key(target_key)}"


def checker_failed_topic(target_key: str) -> str:
    return f"events.checker.failed.{_validate_target_key(target_key)}"


def ceau_event_routed_topic(target_key: str) -> str:
    return f"events.ceau.routed.{_validate_target_key(target_key)}"


def ceau_unit_opened_topic(target_key: str) -> str:
    return f"events.ceau.unit_opened.{_validate_target_key(target_key)}"


def ceau_unit_appended_topic(target_key: str) -> str:
    return f"events.ceau.unit_appended.{_validate_target_key(target_key)}"


def ceau_unit_emitted_topic(target_key: str) -> str:
    return f"events.ceau.unit_emitted.{_validate_target_key(target_key)}"


def ceau_unit_closed_topic(target_key: str) -> str:
    return f"events.ceau.unit_closed.{_validate_target_key(target_key)}"


def ceau_watermark_observed_topic(target_key: str) -> str:
    return f"events.ceau.watermark_observed.{_validate_target_key(target_key)}"


def ceau_source_completeness_observed_topic(target_key: str) -> str:
    return f"events.ceau.source_completeness_observed.{_validate_target_key(target_key)}"


def analysis_requested_topic(target_key: str) -> str:
    return f"commands.analysis.requested.{_validate_target_key(target_key)}"


def analysis_outcome_topic(target_key: str) -> str:
    return f"events.analysis.outcome.{_validate_target_key(target_key)}"


def analysis_failed_topic(target_key: str) -> str:
    return f"events.analysis.failed.{_validate_target_key(target_key)}"


def pm_review_requested_topic(target_key: str) -> str:
    return f"commands.pm_review.requested.{_validate_target_key(target_key)}"


def pm_review_skipped_topic(target_key: str) -> str:
    return f"events.pm_review.skipped.{_validate_target_key(target_key)}"


def pm_review_completed_topic(target_key: str) -> str:
    return f"events.pm_review.completed.{_validate_target_key(target_key)}"


def pm_review_failed_topic(target_key: str) -> str:
    return f"events.pm_review.failed.{_validate_target_key(target_key)}"


def runtime_dead_letter_topic(component: str) -> str:
    return f"runtime.dead_letter.{_validate_component(component)}"


def component_heartbeat_topic(component: str) -> str:
    return f"runtime.component_heartbeat.{_validate_component(component)}"


def news_raw_wildcard() -> str:
    return "data.news.raw.*"


def web_result_raw_wildcard() -> str:
    return "data.web_result.raw.*"


def evidence_admitted_wildcard() -> str:
    return "events.evidence.admitted.*"


def checker_decision_wildcard() -> str:
    return "events.checker.decision.*"


def analysis_requested_wildcard() -> str:
    return "commands.analysis.requested.*"


def analysis_outcome_wildcard() -> str:
    return "events.analysis.outcome.*"


def pm_review_requested_wildcard() -> str:
    return "commands.pm_review.requested.*"


def pm_review_completed_wildcard() -> str:
    return "events.pm_review.completed.*"


def _validate_target_key(value: str) -> str:
    return validate_target_key(value, error_type=RuntimeTopicError)


def _validate_component(value: str) -> str:
    return validate_page_key(
        value,
        field_name="component",
        error_type=RuntimeTopicError,
    )


__all__ = [
    "RuntimeTopicError",
    "analysis_failed_topic",
    "analysis_outcome_wildcard",
    "analysis_outcome_topic",
    "analysis_requested_topic",
    "analysis_requested_wildcard",
    "ceau_event_routed_topic",
    "ceau_source_completeness_observed_topic",
    "ceau_unit_appended_topic",
    "ceau_unit_closed_topic",
    "ceau_unit_emitted_topic",
    "ceau_unit_opened_topic",
    "ceau_watermark_observed_topic",
    "checker_decision_topic",
    "checker_decision_wildcard",
    "checker_failed_topic",
    "checker_requested_topic",
    "component_heartbeat_topic",
    "evidence_admitted_topic",
    "evidence_admitted_wildcard",
    "evidence_quarantined_topic",
    "news_raw_topic",
    "news_raw_wildcard",
    "pm_review_completed_topic",
    "pm_review_completed_wildcard",
    "pm_review_failed_topic",
    "pm_review_requested_topic",
    "pm_review_requested_wildcard",
    "pm_review_skipped_topic",
    "runtime_dead_letter_topic",
    "web_result_raw_topic",
    "web_result_raw_wildcard",
]
