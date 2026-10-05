"""Runtime-facing active exposure resolver for new PMReview paths."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.contracts.view_state_change import (
    ViewState,
    canonical_view_state_target_weight,
)
from event_trader.migrations.cutover_baseline import (
    CUTOVER_RUNTIME_SCHEMA_VERSION,
    DEFAULT_CUTOVER_MIGRATION_ID,
    CutoverMigrationError,
    read_cutover_baseline_payload,
    validate_cutover_baseline,
)
from event_trader.portfolio.contracts import PortfolioState
from event_trader.portfolio.store import PortfolioStateStore, PortfolioStoreError
from event_trader.storage import WorkspaceLayout

ActiveExposureSource = Literal["portfolio_state", "cutover_baseline"]
CutoverBaselineSource = Literal[
    "portfolio_state",
    "execution_record",
    "explicit_flat",
]


class ActiveExposureResolverError(ValueError):
    """Raised when active exposure cannot be resolved for new PMReview paths."""


@dataclass(frozen=True, slots=True)
class ActiveExposure:
    """Authoritative current exposure visible to PMReview and future PM runtime."""

    target_key: str
    state: ViewState
    target_weight: float
    source: ActiveExposureSource
    source_ids: Mapping[str, object]
    source_updated_at: datetime | None
    baseline_source: CutoverBaselineSource | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=ActiveExposureResolverError,
            ),
        )
        if self.state not in {"flat", "weak_long", "strong_long", "weak_short", "strong_short"}:
            raise ActiveExposureResolverError("active exposure state is invalid.")
        object.__setattr__(self, "state", cast(ViewState, self.state))
        if isinstance(self.target_weight, bool) or not isinstance(self.target_weight, int | float):
            raise ActiveExposureResolverError("active exposure target_weight must be numeric.")
        target_weight = float(self.target_weight)
        if target_weight != canonical_view_state_target_weight(self.state):
            raise ActiveExposureResolverError(
                "active exposure target_weight must match state."
            )
        object.__setattr__(self, "target_weight", target_weight)
        if self.source not in {"portfolio_state", "cutover_baseline"}:
            raise ActiveExposureResolverError("active exposure source is invalid.")
        object.__setattr__(self, "source", cast(ActiveExposureSource, self.source))
        if not isinstance(self.source_ids, Mapping):
            raise ActiveExposureResolverError("active exposure source_ids must be a mapping.")
        object.__setattr__(self, "source_ids", dict(self.source_ids))
        if self.source_updated_at is not None:
            object.__setattr__(
                self,
                "source_updated_at",
                validate_timestamp(
                    self.source_updated_at,
                    field_name="source_updated_at",
                    error_type=ActiveExposureResolverError,
                ),
            )
        if self.source == "portfolio_state" and self.baseline_source is not None:
            raise ActiveExposureResolverError(
                "portfolio_state exposure must not set baseline_source."
            )
        if self.source == "cutover_baseline":
            if self.baseline_source not in {
                "portfolio_state",
                "execution_record",
                "explicit_flat",
            }:
                raise ActiveExposureResolverError(
                    "cutover baseline exposure requires a valid baseline_source."
                )
            object.__setattr__(
                self,
                "baseline_source",
                cast(CutoverBaselineSource, self.baseline_source),
            )


def validate_active_exposure_runtime(
    *,
    layout: WorkspaceLayout,
    migration_id: str = DEFAULT_CUTOVER_MIGRATION_ID,
    expected_schema_version: str = CUTOVER_RUNTIME_SCHEMA_VERSION,
) -> None:
    """Fail loudly unless new-runtime schema and cutover baseline are present."""

    try:
        validate_cutover_baseline(
            layout=layout,
            migration_id=migration_id,
            expected_schema_version=expected_schema_version,
        )
    except CutoverMigrationError as exc:
        raise ActiveExposureResolverError(str(exc)) from exc


def resolve_active_exposure(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    migration_id: str = DEFAULT_CUTOVER_MIGRATION_ID,
    expected_schema_version: str = CUTOVER_RUNTIME_SCHEMA_VERSION,
) -> ActiveExposure:
    """Resolve current exposure from PortfolioState or validated cutover baseline."""

    _validate_layout(layout)
    normalized_target = validate_target_key(
        target_key,
        error_type=ActiveExposureResolverError,
    )
    validate_active_exposure_runtime(
        layout=layout,
        migration_id=migration_id,
        expected_schema_version=expected_schema_version,
    )
    try:
        portfolio_state = PortfolioStateStore(layout).read(target_key=normalized_target)
    except PortfolioStoreError as exc:
        raise ActiveExposureResolverError(str(exc)) from exc
    if portfolio_state is not None:
        return _from_portfolio_state(portfolio_state)
    return _from_cutover_baseline(
        layout=layout,
        target_key=normalized_target,
        migration_id=migration_id,
        expected_schema_version=expected_schema_version,
    )


def _from_portfolio_state(state: PortfolioState) -> ActiveExposure:
    source_ids = {
        "source_pm_decision_id": state.source_pm_decision_id,
        "source_execution_record_id": state.source_execution_record_id,
        "decision_episode_id": state.decision_episode_id,
    }
    return ActiveExposure(
        target_key=state.target_key,
        state=state.state,
        target_weight=state.target_weight,
        source="portfolio_state",
        source_ids=source_ids,
        source_updated_at=state.updated_at,
    )


def _from_cutover_baseline(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    migration_id: str,
    expected_schema_version: str,
) -> ActiveExposure:
    payload = read_cutover_baseline_payload(
        layout=layout,
        migration_id=migration_id,
        expected_schema_version=expected_schema_version,
    )
    targets = payload.get("targets")
    if not isinstance(targets, list):
        raise ActiveExposureResolverError("cutover baseline targets must be a list.")
    for item in targets:
        if not isinstance(item, dict):
            continue
        if item.get("target_key") != target_key:
            continue
        source_type = item.get("source_type")
        if source_type not in {
            "portfolio_state",
            "execution_record",
            "explicit_flat",
        }:
            raise ActiveExposureResolverError("cutover baseline source_type is invalid.")
        source_ids = item.get("source_ids")
        if not isinstance(source_ids, Mapping):
            raise ActiveExposureResolverError("cutover baseline source_ids must be a mapping.")
        return ActiveExposure(
            target_key=target_key,
            state=cast(ViewState, item.get("state")),
            target_weight=_require_weight(item.get("target_weight")),
            source="cutover_baseline",
            source_ids=dict(source_ids),
            source_updated_at=_parse_optional_timestamp(item.get("source_updated_at")),
            baseline_source=cast(CutoverBaselineSource, source_type),
        )
    raise ActiveExposureResolverError(
        f"cutover baseline does not include target_key: {target_key}"
    )


def _require_weight(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ActiveExposureResolverError("cutover baseline target_weight must be numeric.")
    return float(value)


def _parse_optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ActiveExposureResolverError("source_updated_at must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ActiveExposureResolverError("source_updated_at must be an ISO timestamp.") from exc
    return validate_timestamp(
        parsed,
        field_name="source_updated_at",
        error_type=ActiveExposureResolverError,
    )


def _validate_layout(layout: WorkspaceLayout) -> None:
    if not isinstance(layout, WorkspaceLayout):
        raise ActiveExposureResolverError("layout must be a WorkspaceLayout instance.")


__all__ = [
    "ActiveExposure",
    "ActiveExposureResolverError",
    "ActiveExposureSource",
    "CutoverBaselineSource",
    "resolve_active_exposure",
    "validate_active_exposure_runtime",
]
