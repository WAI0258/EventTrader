"""Typed material price-level roles for analysis and PM-review contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Literal, cast

from event_trader.contracts._validators import (
    normalize_content,
    validate_event_id,
    validate_target_key,
)
from event_trader.contracts.analysis_assessment_schema import (
    allowed_confidence_values,
    allowed_material_level_roles,
    allowed_price_level_source_types,
    price_level_role_required_fields,
)
from event_trader.contracts.instrument_basis import canonicalize_instrument_basis

MaterialLevelRole = Literal[
    "entry",
    "add",
    "hold_boundary",
    "de_risk_or_take_profit",
    "exit",
    "reverse_or_cover",
    "invalidates",
    "watch_only",
    "not_relevant",
]
PriceLevelSourceType = Literal[
    "source_quoted",
    "market_bar_derived",
    "agent_hypothesis",
    "round_number",
]
Confidence = Literal["low", "medium", "high"]

_MATERIAL_LEVEL_ROLES = frozenset(allowed_material_level_roles())
_PRICE_LEVEL_SOURCE_TYPES = frozenset(allowed_price_level_source_types())
_CONFIDENCE_VALUES = frozenset(allowed_confidence_values())


class PriceLevelRoleContractError(ValueError):
    """Raised when a material price-level role is malformed."""


@dataclass(frozen=True, slots=True)
class PriceLevelRole:
    level_id: str
    target_key: str

    value: float | None
    lower: float | None
    upper: float | None
    instrument_basis: str

    source_type: PriceLevelSourceType
    source_event_ids: tuple[str, ...]
    source_refs: tuple[str, ...]

    role_if_flat: MaterialLevelRole
    role_if_already_long: MaterialLevelRole
    role_if_already_short: MaterialLevelRole

    path_context_required: bool
    refresh_triggers: tuple[str, ...]
    invalidation_triggers: tuple[str, ...]

    confidence: Confidence
    rationale_md: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "level_id", _validate_non_blank(self.level_id, "level_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=PriceLevelRoleContractError),
        )
        object.__setattr__(self, "value", _validate_optional_float(self.value, "value"))
        object.__setattr__(self, "lower", _validate_optional_float(self.lower, "lower"))
        object.__setattr__(self, "upper", _validate_optional_float(self.upper, "upper"))
        if self.value is None and (self.lower is None or self.upper is None):
            raise PriceLevelRoleContractError(
                "price level requires value or a complete lower/upper zone."
            )
        if (self.lower is None) != (self.upper is None):
            raise PriceLevelRoleContractError("lower and upper must be set together.")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise PriceLevelRoleContractError("lower must be less than or equal to upper.")
        object.__setattr__(
            self,
            "instrument_basis",
            canonicalize_instrument_basis(
                _validate_non_blank(self.instrument_basis, "instrument_basis")
            ),
        )
        object.__setattr__(self, "source_type", _validate_source_type(self.source_type))
        object.__setattr__(
            self,
            "source_event_ids",
            _validate_event_ids(
                self.source_event_ids,
                allow_empty=self.source_type in {"agent_hypothesis", "round_number"},
            ),
        )
        object.__setattr__(
            self,
            "source_refs",
            _validate_string_tuple(self.source_refs, "source_refs", allow_empty=True),
        )
        object.__setattr__(
            self,
            "role_if_flat",
            _validate_role(self.role_if_flat, "role_if_flat"),
        )
        object.__setattr__(
            self,
            "role_if_already_long",
            _validate_role(self.role_if_already_long, "role_if_already_long"),
        )
        object.__setattr__(
            self,
            "role_if_already_short",
            _validate_role(self.role_if_already_short, "role_if_already_short"),
        )
        if not isinstance(self.path_context_required, bool):
            raise PriceLevelRoleContractError("path_context_required must be a boolean.")
        object.__setattr__(
            self,
            "refresh_triggers",
            _validate_string_tuple(
                self.refresh_triggers,
                "refresh_triggers",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "invalidation_triggers",
            _validate_string_tuple(
                self.invalidation_triggers,
                "invalidation_triggers",
                allow_empty=True,
            ),
        )
        object.__setattr__(self, "confidence", _validate_confidence(self.confidence))
        object.__setattr__(
            self,
            "rationale_md",
            normalize_content(
                self.rationale_md,
                field_name="rationale_md",
                error_type=PriceLevelRoleContractError,
            ),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "level_id": self.level_id,
            "target_key": self.target_key,
            "value": self.value,
            "lower": self.lower,
            "upper": self.upper,
            "instrument_basis": self.instrument_basis,
            "source_type": self.source_type,
            "source_event_ids": list(self.source_event_ids),
            "source_refs": list(self.source_refs),
            "role_if_flat": self.role_if_flat,
            "role_if_already_long": self.role_if_already_long,
            "role_if_already_short": self.role_if_already_short,
            "path_context_required": self.path_context_required,
            "refresh_triggers": list(self.refresh_triggers),
            "invalidation_triggers": list(self.invalidation_triggers),
            "confidence": self.confidence,
            "rationale_md": self.rationale_md,
        }


def parse_price_level_role(payload: Mapping[str, object]) -> PriceLevelRole:
    _require_exact_fields(
        payload,
        required=set(price_level_role_required_fields()),
        surface="price_level_role",
    )
    return PriceLevelRole(
        level_id=_require_text(payload, "level_id"),
        target_key=_require_text(payload, "target_key"),
        value=_optional_float(payload.get("value")),
        lower=_optional_float(payload.get("lower")),
        upper=_optional_float(payload.get("upper")),
        instrument_basis=_require_text(payload, "instrument_basis"),
        source_type=cast(PriceLevelSourceType, _require_text(payload, "source_type")),
        source_event_ids=tuple(_require_string_list(payload, "source_event_ids")),
        source_refs=tuple(_require_string_list(payload, "source_refs")),
        role_if_flat=cast(MaterialLevelRole, _require_text(payload, "role_if_flat")),
        role_if_already_long=cast(
            MaterialLevelRole,
            _require_text(payload, "role_if_already_long"),
        ),
        role_if_already_short=cast(
            MaterialLevelRole,
            _require_text(payload, "role_if_already_short"),
        ),
        path_context_required=_require_bool(payload, "path_context_required"),
        refresh_triggers=tuple(_require_string_list(payload, "refresh_triggers")),
        invalidation_triggers=tuple(_require_string_list(payload, "invalidation_triggers")),
        confidence=cast(Confidence, _require_text(payload, "confidence")),
        rationale_md=_require_text(payload, "rationale_md"),
    )


def is_semantically_active_price_level(level: PriceLevelRole) -> bool:
    return (
        not (
            level.role_if_flat == "not_relevant"
            and level.role_if_already_long == "not_relevant"
            and level.role_if_already_short == "not_relevant"
        )
        or bool(level.refresh_triggers)
        or bool(level.invalidation_triggers)
        or level.path_context_required
    )


def _validate_non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PriceLevelRoleContractError(f"{field_name} must be a non-blank string.")
    normalized = value.strip()
    if normalized != value:
        raise PriceLevelRoleContractError(
            f"{field_name} must not include leading or trailing whitespace."
        )
    return normalized


def _validate_optional_float(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PriceLevelRoleContractError(f"{field_name} must be numeric when set.")
    normalized = float(value)
    if not isfinite(normalized):
        raise PriceLevelRoleContractError(f"{field_name} must be finite.")
    return normalized


def _validate_event_ids(
    values: tuple[str, ...],
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    normalized = tuple(values)
    if not normalized and not allow_empty:
        raise PriceLevelRoleContractError(
            "source_event_ids must not be empty for source-quoted or market-derived levels."
        )
    seen: set[str] = set()
    for value in normalized:
        validated = validate_event_id(value, error_type=PriceLevelRoleContractError)
        if validated in seen:
            raise PriceLevelRoleContractError("source_event_ids must not contain duplicates.")
        seen.add(validated)
    return normalized


def _validate_string_tuple(
    values: tuple[str, ...],
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    normalized = tuple(values)
    if not normalized and not allow_empty:
        raise PriceLevelRoleContractError(f"{field_name} must not be empty.")
    seen: set[str] = set()
    for value in normalized:
        text = _validate_non_blank(value, field_name)
        if text in seen:
            raise PriceLevelRoleContractError(f"{field_name} must not contain duplicates.")
        seen.add(text)
    return normalized


def _validate_role(value: object, field_name: str) -> MaterialLevelRole:
    if value not in _MATERIAL_LEVEL_ROLES:
        allowed_values = ", ".join(sorted(_MATERIAL_LEVEL_ROLES))
        raise PriceLevelRoleContractError(
            f"{field_name} must be one of: {allowed_values}; actual={value!r}."
        )
    return cast(MaterialLevelRole, value)


def _validate_source_type(value: object) -> PriceLevelSourceType:
    if value not in _PRICE_LEVEL_SOURCE_TYPES:
        raise PriceLevelRoleContractError("source_type is not supported.")
    return cast(PriceLevelSourceType, value)


def _validate_confidence(value: object) -> Confidence:
    if value not in _CONFIDENCE_VALUES:
        raise PriceLevelRoleContractError("confidence must be low, medium, or high.")
    return cast(Confidence, value)


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    return _validate_non_blank(payload.get(field_name), field_name)


def _optional_float(value: object) -> float | None:
    return _validate_optional_float(value, "optional_float")


def _require_bool(payload: Mapping[str, object], field_name: str) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise PriceLevelRoleContractError(f"{field_name} must be a boolean.")
    return value


def _require_string_list(payload: Mapping[str, object], field_name: str) -> list[str]:
    value = payload.get(field_name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PriceLevelRoleContractError(f"{field_name} must be a list of strings.")
    return value


def _require_exact_fields(
    payload: Mapping[str, object],
    *,
    required: set[str],
    surface: str,
) -> None:
    actual = set(str(key) for key in payload)
    missing = sorted(required - actual)
    unexpected = sorted(actual - required)
    if missing or unexpected:
        problems: list[str] = []
        if missing:
            problems.append(f"missing fields: {', '.join(missing)}")
        if unexpected:
            problems.append(f"unexpected fields: {', '.join(unexpected)}")
        raise PriceLevelRoleContractError(f"{surface} " + "; ".join(problems))


__all__ = [
    "Confidence",
    "MaterialLevelRole",
    "PriceLevelRole",
    "PriceLevelRoleContractError",
    "PriceLevelSourceType",
    "is_semantically_active_price_level",
    "parse_price_level_role",
]
